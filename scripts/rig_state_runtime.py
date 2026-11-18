"""Bounded, explicit-time playback of selected, executable catalog states.

This in-memory scheduler is intentionally separate from durable rig jobs. It
never generates missing clips or turns numerical playback into visual approval.
"""
from copy import deepcopy
from contextlib import contextmanager
from fractions import Fraction
import math
from sys import float_info
import numpy as np
from rig_animation import (evaluate_animation, evaluate_track, blend_poses, contact_is_active,
                           apply_animation_controls)
from rig_constraints import apply_joint_limits
from rig_errors import RigError, require, object_hash
from rig_skeleton import evaluate_fk
from rig_skinning import evaluate_deformation
from rig_states import map_states


class RigStateRuntime:
    """Play a selected mapping; callers provide monotonic seconds explicitly.

    Requests use the incoming state's interruption policy, as catalog event
    mappings do. Cancellation is an explicit override to selected static-neutral.
    A transition freezes its evaluated source and its pre-control input. Blends
    operate on pre-control inputs before one native control pass, without a
    recursive blend tree. Root translation is aligned to the evaluated source.
    Contact-free release fades blend the actual source toward a controlled target.
    """

    def __init__(self, rig, initial, *, recipes=None, selected=None,
                 blend_seconds=0.2, now=0.0, transition_budget=32,
                 control_transition='continuous'):
        require(isinstance(now, (int, float)) and not isinstance(now, bool) and
                0 <= now <= float_info.max and float(now) == now,
                'RIG_STATE_TIME', 'Runtime time must be finite, nonnegative and exactly representable by its float clock.')
        require(isinstance(blend_seconds, (int, float)) and not isinstance(blend_seconds, bool)
                and 0 <= blend_seconds <= 10,
                'RIG_STATE_BLEND', 'Blend duration must be within zero to ten seconds.')
        require(type(transition_budget) is int and 1 <= transition_budget <= 1024,
                'RIG_STATE_BUDGET', 'Transition budget must be within 1..1024.')
        require(type(control_transition) is str and control_transition in ('continuous', 'project'),
                'RIG_STATE_CONTACT_TRANSITION',
                'Control transition must be continuous or explicitly project incoming controls.')
        self.rig = rig
        self.mapping = map_states(rig, recipes=recipes, selected=selected)
        self.recipes = deepcopy(self.mapping['recipes'])
        self._require_state(initial)
        self.state = initial
        self.entered = self.time = float(now)
        self.blend_seconds = float(blend_seconds)
        self.transition_budget = transition_budget
        self.control_transition = control_transition
        self.transition_count = 0
        self.source_pose = None
        self._input_pose = self._source_input_pose = None
        self._source_contact_correction = False
        self.root_offset = np.zeros(3)
        self._signature = self._current_signature()

    def _current_signature(self):
        return object_hash(dict(manifest=self.rig.manifest, clips=self.rig.clips))

    def _check(self, now):
        require(isinstance(now, (int, float)) and not isinstance(now, bool)
                and self.time <= now <= float_info.max and float(now) == now,
                'RIG_STATE_TIME', 'Runtime time must be finite, monotonic and exactly representable by its float clock.')
        require(self._signature == self._current_signature(), 'RIG_STATE_STALE',
                'Rig or animation changed; create a fresh runtime mapping.')

    def _require_state(self, state):
        require(isinstance(state, str) and state in self.recipes, 'RIG_STATE_UNAVAILABLE',
                'State has no selected executable clip recipe.')

    def _duration(self):
        # Round the complete sum once, just as each interval prefix below.
        # A tiny final clip must still own a completed recipe's endpoint.
        return float(sum((Fraction(self.rig.clips[c]['duration'])
                          for c in self.recipes[self.state]['clips']), Fraction()))

    def _boundary(self, elapsed):
        # Positive intervals cannot complete at their own entry timestamp,
        # even when their duration is smaller than that timestamp's spacing.
        return max(self.entered+elapsed, math.nextafter(self.entered, math.inf))

    def _root(self, pose):
        name = self.rig.skeleton['root']
        rest = next(b['rest']['translation'] for b in self.rig.skeleton['bones'] if b['id'] == name)
        return np.asarray(pose['bones'].get(name, {}).get('translation', rest))

    def _authored_root(self, clip, time):
        # Trajectory uses authored channels, just as native loop accumulation
        # does. Constraints/IK belong to the final placed pose, not its offset.
        # Future non-root tracks need not produce an executable full pose just
        # to describe this translation; actual sampled poses are admitted below.
        name = self.rig.skeleton['root']
        track = next((track for track in clip['tracks']
                      if track['target'] == name and track['path'] == 'translation'), None)
        if track is not None:
            return evaluate_track(track, time)
        return np.asarray(next(b['rest']['translation'] for b in self.rig.skeleton['bones'] if b['id'] == name))

    def _sample(self, now):
        recipe = self.recipes[self.state]
        elapsed = max(0., now-self.entered)
        clips = recipe['clips']
        duration = self._duration()
        looping = recipe['completion'] is None and self.rig.clips[clips[-1]]['loop']
        completed = not looping and now >= self._boundary(duration)
        holding = completed and recipe['completion'] is not None
        if not looping and recipe['completion'] is not None:
            # A scheduled endpoint is authoritative: subtraction can round it
            # back into the interior. Holding also freezes an unfinished fade.
            elapsed = duration if completed else min(elapsed, duration)
        blend_elapsed = elapsed
        if now >= self._boundary(self.blend_seconds) and not (holding and duration < self.blend_seconds):
            blend_elapsed = max(blend_elapsed, self.blend_seconds)
        cycles = elapsed//duration if looping else 0.
        local = elapsed % duration if looping else min(elapsed, duration)
        absolute_intervals = not looping
        cycle_offset = 0.
        if looping and math.isfinite(cycles):
            original_cycles = cycles
            cycle_start = self.entered+cycles*duration
            cycle_end = self.entered+(cycles+1)*duration
            if cycle_end > cycle_start:
                # Correct cancellation at a representable complete-cycle edge.
                if now >= cycle_end:
                    cycles += 1
                elif now < cycle_start and cycles > 0:
                    cycles -= 1
                if cycles != original_cycles:
                    local = max(0., min(math.nextafter(duration, -math.inf),
                                       local+(original_cycles-cycles)*duration))
                cycle_offset = cycles*duration
                absolute_intervals = True
        phase = local
        cycle_delta = np.zeros(3)
        prior_duration = 0.
        exact_duration = Fraction()
        first_root = self._authored_root(self.rig.clips[clips[0]], 0)
        for index, name in enumerate(clips):
            clip = self.rig.clips[name]
            start = self._authored_root(clip, 0)
            end = self._authored_root(clip, clip['duration'])
            delta = end-start if clip['root_motion'] == 'embedded' else np.zeros(3)
            exact_duration += Fraction(clip['duration'])
            end_duration = float(exact_duration)
            boundary = self._boundary(cycle_offset+end_duration)
            selected = not completed and (now < boundary if absolute_intervals else local < clip['duration'])
            if selected or index == len(clips)-1:
                if completed:
                    local = clip['duration']
                elif absolute_intervals:
                    # Rounded absolute boundaries decide interval ownership,
                    # but cannot replace the elapsed/remainder clock inside it.
                    # Subtracting a rounded large-origin cycle start changes
                    # interior key selection and can hide invalid samples.
                    local = max(0., phase-prior_duration)
                    if now == self.entered+(cycle_offset+prior_duration):
                        local = 0.
                # Every interval is half-open except a known completed final
                # non-looping clip. Equivalent floating-point sums can disagree
                # at a loop edge, without making its terminal STEP key visible.
                if not completed:
                    local = min(local, math.nextafter(clip['duration'], -math.inf))
                sampled_root = self._authored_root(clip, min(local, clip['duration']))
                prior_delta = cycle_delta.copy()
                break
            local -= clip['duration']
            cycle_delta += delta
            prior_duration = end_duration
        total_delta = sum((self._authored_root(self.rig.clips[c], self.rig.clips[c]['duration'])
                           - self._authored_root(self.rig.clips[c], 0) for c in clips if self.rig.clips[c]['root_motion'] == 'embedded'), np.zeros(3))
        motion_error = 'Requested runtime time exceeds finite root-motion representation.'
        with np.errstate(over='ignore', invalid='ignore'):
            if math.isfinite(cycles):
                repeated_delta = cycles*total_delta
            else:
                # The cycle count can overflow even when its displacement fits.
                require(np.isfinite(total_delta).all(), 'RIG_STATE_TIME', motion_error)
                exact_cycles = Fraction(elapsed)//Fraction(duration)
                try:
                    repeated_delta = np.array([float(exact_cycles*Fraction(float(delta))) for delta in total_delta])
                except OverflowError as exc:
                    raise RigError('RIG_STATE_TIME', motion_error) from exc
            translation = first_root+sampled_root-start+self.root_offset+prior_delta+repeated_delta
            placement = first_root-start+self.root_offset+prior_delta+repeated_delta
        require(np.isfinite(translation).all() and np.isfinite(placement).all(), 'RIG_STATE_TIME', motion_error)
        # Place the root and contact targets before native constraints and IK.
        # Translating an already solved pose changes its relation to fixed world
        # poles/constraint points, so matching the endpoint alone is insufficient.
        sampled = evaluate_animation(self.rig, clip, min(local, clip['duration']),
                                     _root_offset=placement, contacts=False, constraints=False)
        input_pose = sampled['pose']
        goals = {contact['chain']: contact for contact in clip['contacts']
                 if contact_is_active(contact, sampled['time'], clip['duration'])}
        blending = (self._source_input_pose is not None and self.blend_seconds > 0
                    and blend_elapsed < self.blend_seconds)
        releasing = blending and self._source_contact_correction and not goals
        control_input = input_pose
        if blending:
            # Complete both poses: sparse target channels must fade old channels
            # back to rest, including morphs omitted from the target clip.
            source, target = deepcopy(self._source_input_pose), deepcopy(input_pose)
            for bone in self.rig.skeleton['bones']:
                for path, rest in bone['rest'].items():
                    target['bones'].setdefault(bone['id'], {}).setdefault(path, deepcopy(rest))
            for mesh in self.rig.geometry.meshes:
                for morph in mesh.morphs:
                    target['morphs'].setdefault(mesh.id, {}).setdefault(morph, mesh.morph_defaults.get(morph, 0.))
            input_pose = blend_poses(self.rig, source, target, blend_elapsed/self.blend_seconds)
            control_input = target if releasing else input_pose
        # Blend before native controls: replaying a fractional aim/copy on
        # already controlled poses changes its influence and snaps at blend end.
        # Incoming authored contacts still override the pose crossfade; never
        # interpolate their goals. Keep the raw input as a bounded snapshot so
        # interruption can resume the same single native pass without inversion.
        controlled = apply_animation_controls(self.rig, clip, control_input, sampled['time'],
                                               contact_offset=placement)
        controlled['control_stage'] = 'native'
        if releasing:
            # With no incoming authored lock, release the outgoing correction
            # from the actual frozen final pose toward the once-controlled
            # target. Reprojecting this blend would replay fractional controls.
            # Interruptions retain this release provenance until the fade ends.
            controlled['pose'] = blend_poses(self.rig, self.source_pose, controlled['pose'],
                                              blend_elapsed/self.blend_seconds)
            controlled['pose'], controlled['constraints_applied'] = apply_joint_limits(
                self.rig.skeleton, controlled['pose'],
                self.rig.manifest['controllers']['constraints'])
            controlled['control_stage'] = 'contact-release-blend'
        # Finite FK can still overflow when applied to valid vertices or inverse
        # binds. Admit every returned/captured runtime sample through the actual
        # bounded native skinning owner before any playback state is committed.
        evaluate_deformation(self.rig, input_pose)
        evaluate_deformation(self.rig, controlled['pose'])
        # Keep the native evaluator's per-solve reports separate; runtime
        # contacts always describe the final geometry after every chain.
        controlled['solve_contacts'] = controlled['contacts']
        controlled['contacts'] = controlled['final_contacts']
        return dict(input_pose=input_pose, **controlled)

    def _pose(self, now):
        return self._sample(now)['pose']

    def _enter(self, state, now, pose, input_pose, *, contact_correction=False):
        require(self.transition_count < self.transition_budget, 'RIG_STATE_BUDGET',
                'Runtime transition budget exhausted; create a new runtime explicitly.')
        self._require_state(state)
        first = self.rig.clips[self.recipes[state]['clips'][0]]
        start = self._authored_root(first, 0)
        offset = self._root(pose)-start
        if self.blend_seconds and self.control_transition == 'continuous':
            active = any(contact_is_active(c, 0., first['duration']) for c in first['contacts'])
            if contact_correction and not active:
                incoming, _ = apply_joint_limits(self.rig.skeleton, pose,
                                                 self.rig.manifest['controllers']['constraints'])
            else:
                incoming = apply_animation_controls(self.rig, first, input_pose, 0.,
                                                     contact_offset=offset)['pose']
            source_fk = evaluate_fk(self.rig.skeleton, pose)
            incoming_fk = evaluate_fk(self.rig.skeleton, incoming)
            require(all(np.allclose(source_fk[bone], incoming_fk[bone], rtol=0, atol=1e-7)
                        for bone in source_fk), 'RIG_STATE_CONTACT_TRANSITION',
                    'Incoming controls change the captured transition pose; use compatible goals, '
                    'an explicit zero-duration transition, or control_transition="project".')
        self.state, self.entered = state, float(now)
        self.source_pose, self.root_offset = deepcopy(pose), offset
        self._source_input_pose = deepcopy(input_pose)
        self._source_contact_correction = contact_correction
        self.transition_count += 1

    @contextmanager
    def _transaction(self):
        # A failed completion chain or request must not consume an attempt or
        # leave a partially advanced active state.
        fields = ('state', 'entered', 'time', 'source_pose', '_input_pose',
                  '_source_input_pose', '_source_contact_correction',
                  'root_offset', 'transition_count')
        before = {name: deepcopy(getattr(self, name)) for name in fields}
        try:
            yield
        except Exception:
            for name, value in before.items():
                setattr(self, name, value)
            raise

    def evaluate(self, now):
        with self._transaction():
            return self._evaluate(now)

    def _evaluate(self, now):
        """Evaluate geometry-driving pose and perform available completion edges."""
        self._check(now)
        status = 'playing'
        while self.recipes[self.state]['completion'] is not None:
            # A short clip at a large timestamp can round back to its start.
            boundary = self._boundary(self._duration())
            if now < boundary:
                break
            target = self.recipes[self.state]['completion']
            if target not in self.recipes:
                status = 'completion-unavailable'
                break
            completed = self._sample(boundary)
            self._enter(target, boundary, completed['pose'], completed['input_pose'],
                        contact_correction=bool(completed['contacts']) or
                        completed['control_stage'] == 'contact-release-blend')
        sampled = self._sample(now)
        self._input_pose = deepcopy(sampled.pop('input_pose'))
        pending = self.recipes[self.state]['completion'] if status == 'completion-unavailable' else None
        if any(not contact['reachable'] or not contact['converged'] for contact in sampled['contacts']):
            status = 'controls-unsatisfied'
        self.time = float(now)
        return dict(state=self.state, elapsed=now-self.entered, **sampled,
                    status=status, transition_count=self.transition_count,
                    pending_completion=pending)

    def request(self, state, now):
        """Request an allowed transition; rejected requests preserve playback."""
        with self._transaction():
            self._check(now)
            self._require_state(state)
            evaluated = self._evaluate(now)
            pose = evaluated['pose']
            current, incoming = self.recipes[self.state], self.recipes[state]
            if state == self.state and incoming['repeat'] == 'coalesce':
                return self._evaluate(now)
            require(state == self.state or state in current['legal_transitions'], 'RIG_STATE_TRANSITION',
                    'Requested state is not a legal transition from the active state.')
            require(incoming['interrupt'] == 'always' or
                    (incoming['interrupt'] == 'higher' and incoming['priority'] > current['priority']),
                    'RIG_STATE_PRIORITY', 'Incoming state cannot interrupt the active priority.')
            self._enter(state, now, pose, self._input_pose,
                        contact_correction=bool(evaluated['contacts']) or
                        evaluated['control_stage'] == 'contact-release-blend')
            return self._evaluate(now)

    def cancel(self, now):
        """Cancel into executable static-neutral, preserving the current pose."""
        with self._transaction():
            self._check(now)
            self._require_state('static-neutral')
            evaluated = self._evaluate(now)
            self._enter('static-neutral', now, evaluated['pose'], self._input_pose,
                        contact_correction=bool(evaluated['contacts']) or
                        evaluated['control_stage'] == 'contact-release-blend')
            return self._evaluate(now)
