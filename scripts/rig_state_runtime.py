"""Bounded, explicit-time playback of selected, executable catalog states.

This in-memory scheduler is intentionally separate from durable rig jobs. It
never generates missing clips or turns numerical playback into visual approval.
"""
from bisect import bisect_right
from copy import deepcopy
from contextlib import contextmanager
from fractions import Fraction
from itertools import accumulate
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
        # Keep represented durations exact across recipe and cycle grouping.
        # Only the final absolute scheduled boundary is rounded to a float.
        return sum((Fraction(self.rig.clips[c]['duration'])
                    for c in self.recipes[self.state]['clips']), Fraction())

    def _boundary(self, elapsed):
        # Positive intervals cannot complete at their own entry timestamp,
        # even when their duration is smaller than that timestamp's spacing.
        try:
            scheduled = float(Fraction(self.entered)+Fraction(elapsed))
        except OverflowError:
            scheduled = math.inf
        return max(scheduled, math.nextafter(self.entered, math.inf))

    def _collapsed_elapsed(self, elapsed, clips, starts):
        # Whole-recipe unrolling must not change the native remainder clock.
        # Use the shortest repeating clip sequence as its represented period.
        count = len(clips)
        for size in range(1, count//2+1):
            if count % size == 0 and clips[:size]*(count//size) == clips:
                count = size
                break
        period = starts[count]
        cycles, phase = divmod(elapsed, Fraction(float(period)))
        phase = float(phase)
        prefixes = [float(start) for start in starts[:count]]
        index = bisect_right(prefixes, phase)-1
        duration = float(starts[index+1]-starts[index])
        local = min(phase-prefixes[index], math.nextafter(duration, -math.inf))
        return cycles*period + starts[index] + Fraction(local)

    def _loop_elapsed(self, now, elapsed, clips, starts):
        # At most one exact interval boundary can own this rounded timestamp.
        # If several boundaries collapse here, use the relative native clock.
        # Looking one boundary beyond each side of the containing interval is
        # sufficient: rounded edges of an increasing schedule are monotonic.
        period = starts[-1]
        count = len(starts)-1
        cycles, phase = divmod(elapsed, period)
        interval = cycles*count + bisect_right(starts, phase)-1
        matched = None
        for edge in range(max(0, interval-1), interval+3):
            cycle, index = divmod(edge, count)
            offset = cycle*period + starts[index]
            boundary = self._boundary(offset) if edge else self.entered
            if boundary == now:
                if matched is not None:
                    return self._collapsed_elapsed(elapsed, clips, starts)
                matched = offset
        return elapsed if matched is None else matched

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

    def _root_placement(self, clips, selected, cycles):
        # Each preceding clip contributes once more than the complete cycles.
        # Accumulate represented coordinates before rounding so cancellation
        # cannot change displacement when the same recipe is unrolled.
        root = self._authored_root(self.rig.clips[clips[0]], 0)
        start = self._authored_root(self.rig.clips[clips[selected]], 0)
        placement = [Fraction(float(a))-Fraction(float(b))+Fraction(float(c))
                     for a, b, c in zip(root, start, self.root_offset)]
        for index, name in enumerate(clips):
            clip = self.rig.clips[name]
            count = cycles + (index < selected)
            if count and clip['root_motion'] == 'embedded':
                initial = self._authored_root(clip, 0)
                final = self._authored_root(clip, clip['duration'])
                for axis in range(3):
                    placement[axis] += count*(Fraction(float(final[axis]))-Fraction(float(initial[axis])))
        try:
            return np.array([float(coordinate) for coordinate in placement])
        except OverflowError as exc:
            raise RigError('RIG_STATE_TIME', 'Requested runtime time exceeds finite root-motion representation.') from exc

    def _sample(self, now):
        recipe = self.recipes[self.state]
        elapsed = max(0., now-self.entered)
        clips = recipe['clips']
        starts = list(accumulate((Fraction(self.rig.clips[name]['duration']) for name in clips),
                                 initial=Fraction()))
        period = starts[-1]
        duration = float(period)
        looping = recipe['completion'] is None and self.rig.clips[clips[-1]]['loop']
        completed = not looping and now >= self._boundary(period)
        holding = completed and recipe['completion'] is not None
        if not looping and recipe['completion'] is not None:
            # A scheduled endpoint is authoritative: subtraction can round it
            # back into the interior. Holding also freezes an unfinished fade.
            elapsed = duration if completed else min(elapsed, duration)
        blend_elapsed = elapsed
        if now >= self._boundary(self.blend_seconds) and not (holding and duration < self.blend_seconds):
            blend_elapsed = max(blend_elapsed, self.blend_seconds)
        exact_elapsed = Fraction(now)-Fraction(self.entered)
        if looping:
            cycles, cycle_phase = divmod(self._loop_elapsed(now, exact_elapsed, clips, starts), period)
        else:
            cycles, cycle_phase = 0, exact_elapsed
        for index, name in enumerate(clips):
            clip = self.rig.clips[name]
            if looping:
                selected = cycle_phase < starts[index+1]
            else:
                selected = not completed and now < self._boundary(starts[index+1])
            if selected or index == len(clips)-1:
                if completed:
                    local = clip['duration']
                elif not looping and index and now == self._boundary(starts[index]):
                    local = 0.
                else:
                    local = max(0., float(cycle_phase-starts[index]))
                # Every interval is half-open except a known completed final
                # non-looping clip. Equivalent floating-point sums can disagree
                # at a loop edge, without making its terminal STEP key visible.
                if not completed:
                    local = min(local, math.nextafter(clip['duration'], -math.inf))
                break
        placement = self._root_placement(clips, index, cycles)
        with np.errstate(over='ignore', invalid='ignore'):
            translation = self._authored_root(clip, local)+placement
        require(np.isfinite(translation).all(), 'RIG_STATE_TIME',
                'Requested runtime time exceeds finite root-motion representation.')
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
