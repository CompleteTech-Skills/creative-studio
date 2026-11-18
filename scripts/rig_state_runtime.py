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
from rig_animation import evaluate_animation
from rig_errors import RigError, require, object_hash
from rig_states import map_states


class RigStateRuntime:
    """Play a selected mapping; callers provide monotonic seconds explicitly.

    Requests use the incoming state's interruption policy, as catalog event
    mappings do. Cancellation is an explicit override to selected static-neutral.
    A transition freezes its source pose, so interrupting it is continuous and
    does not grow a recursive blend tree. Root translation is aligned at entry.
    """

    def __init__(self, rig, initial, *, recipes=None, selected=None,
                 blend_seconds=0.2, now=0.0, transition_budget=32):
        require(isinstance(now, (int, float)) and not isinstance(now, bool) and 0 <= now <= float_info.max,
                'RIG_STATE_TIME', 'Runtime time must be finite and nonnegative.')
        require(isinstance(blend_seconds, (int, float)) and not isinstance(blend_seconds, bool)
                and 0 <= blend_seconds <= 10,
                'RIG_STATE_BLEND', 'Blend duration must be within zero to ten seconds.')
        require(type(transition_budget) is int and 1 <= transition_budget <= 1024,
                'RIG_STATE_BUDGET', 'Transition budget must be within 1..1024.')
        self.rig = rig
        self.mapping = map_states(rig, recipes=recipes, selected=selected)
        self.recipes = deepcopy(self.mapping['recipes'])
        self._require_state(initial)
        self.state = initial
        self.entered = self.time = float(now)
        self.blend_seconds = float(blend_seconds)
        self.transition_budget = transition_budget
        self.transition_count = 0
        self.source_pose = None
        self.root_offset = np.zeros(3)
        self._signature = self._current_signature()

    def _current_signature(self):
        return object_hash(dict(manifest=self.rig.manifest, clips=self.rig.clips))

    def _check(self, now):
        require(isinstance(now, (int, float)) and not isinstance(now, bool)
                and self.time <= now <= float_info.max,
                'RIG_STATE_TIME', 'Runtime time must be finite and monotonic.')
        require(self._signature == self._current_signature(), 'RIG_STATE_STALE',
                'Rig or animation changed; create a fresh runtime mapping.')

    def _require_state(self, state):
        require(isinstance(state, str) and state in self.recipes, 'RIG_STATE_UNAVAILABLE',
                'State has no selected executable clip recipe.')

    def _duration(self):
        return sum(self.rig.clips[c]['duration'] for c in self.recipes[self.state]['clips'])

    def _root(self, pose):
        name = self.rig.skeleton['root']
        rest = next(b['rest']['translation'] for b in self.rig.skeleton['bones'] if b['id'] == name)
        return np.asarray(pose['bones'].get(name, {}).get('translation', rest))

    def _authored_root(self, clip, time):
        # Trajectory uses authored channels, just as native loop accumulation
        # does. Constraints/IK belong to the final placed pose, not its offset.
        pose = evaluate_animation(self.rig, clip, time,
                                  contacts=False, constraints=False)['pose']
        return self._root(pose)

    def _sample(self, now):
        recipe = self.recipes[self.state]
        elapsed = max(0., now-self.entered)
        clips = recipe['clips']
        duration = self._duration()
        looping = recipe['completion'] is None and self.rig.clips[clips[-1]]['loop']
        cycles = elapsed//duration if looping else 0.
        local = elapsed % duration if looping else min(elapsed, duration)
        cycle_delta = np.zeros(3)
        first_root = self._authored_root(self.rig.clips[clips[0]], 0)
        for index, name in enumerate(clips):
            clip = self.rig.clips[name]
            start = self._authored_root(clip, 0)
            end = self._authored_root(clip, clip['duration'])
            delta = end-start if clip['root_motion'] == 'embedded' else np.zeros(3)
            if local < clip['duration'] or index == len(clips)-1:
                sampled_root = self._authored_root(clip, min(local, clip['duration']))
                prior_delta = cycle_delta.copy()
                break
            local -= clip['duration']
            cycle_delta += delta
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
        blending = self.source_pose is not None and self.blend_seconds and elapsed < self.blend_seconds
        return evaluate_animation(self.rig, clip, min(local, clip['duration']),
                                  _root_offset=placement,
                                  _blend_from=self.source_pose if blending else None,
                                  _blend_weight=elapsed/self.blend_seconds if blending else 1.)

    def _pose(self, now):
        return self._sample(now)['pose']

    def _enter(self, state, now, pose):
        require(self.transition_count < self.transition_budget, 'RIG_STATE_BUDGET',
                'Runtime transition budget exhausted; create a new runtime explicitly.')
        self._require_state(state)
        first = self.rig.clips[self.recipes[state]['clips'][0]]
        start = self._authored_root(first, 0)
        offset = self._root(pose)-start
        self.state, self.entered = state, float(now)
        self.source_pose, self.root_offset = deepcopy(pose), offset
        self.transition_count += 1

    @contextmanager
    def _transaction(self):
        # A failed completion chain or request must not consume an attempt or
        # leave a partially advanced active state.
        fields = ('state', 'entered', 'time', 'source_pose', 'root_offset', 'transition_count')
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
            boundary = max(self.entered+self._duration(), math.nextafter(self.entered, math.inf))
            if now < boundary:
                break
            target = self.recipes[self.state]['completion']
            if target not in self.recipes:
                status = 'completion-unavailable'
                break
            self._enter(target, boundary, self._pose(boundary))
        sample = self._sample(now)
        completion_unavailable = status == 'completion-unavailable'
        if any(not report['reachable'] or not report['converged'] for report in sample['contacts']):
            status = 'controls-unsatisfied'
        self.time = float(now)
        return dict(state=self.state, elapsed=now-self.entered, pose=sample['pose'],
                    contacts=deepcopy(sample['contacts']),
                    constraints_applied=deepcopy(sample['constraints_applied']),
                    status=status, transition_count=self.transition_count,
                    pending_completion=self.recipes[self.state]['completion'] if completion_unavailable else None)

    def request(self, state, now):
        """Request an allowed transition; rejected requests preserve playback."""
        with self._transaction():
            self._check(now)
            self._require_state(state)
            pose = self._evaluate(now)['pose']
            current, incoming = self.recipes[self.state], self.recipes[state]
            if state == self.state and incoming['repeat'] == 'coalesce':
                return self._evaluate(now)
            require(state == self.state or state in current['legal_transitions'], 'RIG_STATE_TRANSITION',
                    'Requested state is not a legal transition from the active state.')
            require(incoming['interrupt'] == 'always' or
                    (incoming['interrupt'] == 'higher' and incoming['priority'] > current['priority']),
                    'RIG_STATE_PRIORITY', 'Incoming state cannot interrupt the active priority.')
            self._enter(state, now, pose)
            return self._evaluate(now)

    def cancel(self, now):
        """Cancel into executable static-neutral, preserving the current pose."""
        with self._transaction():
            self._check(now)
            self._require_state('static-neutral')
            pose = self._evaluate(now)['pose']
            self._enter('static-neutral', now, pose)
            return self._evaluate(now)
