"""Bounded, explicit-time playback of selected, executable catalog states.

This in-memory scheduler is intentionally separate from durable rig jobs. It
never generates missing clips or turns numerical playback into visual approval.
"""
from copy import deepcopy
from contextlib import contextmanager
import math
import numpy as np
from rig_animation import evaluate_animation, blend_poses
from rig_contracts import validate_contract
from rig_skinning import evaluate_deformation
from rig_errors import require, object_hash
from rig_states import map_states


class RigStateRuntime:
    """Play a selected mapping; callers provide monotonic seconds explicitly.

    Requests use the incoming state's interruption policy, as catalog event
    mappings do. Cancellation is an explicit override to selected static-neutral.
    A transition freezes its source pose, so interrupting it is continuous and
    does not grow a recursive blend tree. Root translation is aligned at entry.
    """

    def __init__(self, rig, initial, *, recipes=None, selected=None,
                 blend_seconds=0.2, now=0.0, transition_budget=32,
                 reduced_motion=False, reduced_motion_poses=None):
        require(isinstance(now, (int, float)) and not isinstance(now, bool) and math.isfinite(now) and now >= 0,
                'RIG_STATE_TIME', 'Runtime time must be finite and nonnegative.')
        require(isinstance(blend_seconds, (int, float)) and not isinstance(blend_seconds, bool)
                and math.isfinite(blend_seconds) and 0 <= blend_seconds <= 10,
                'RIG_STATE_BLEND', 'Blend duration must be within zero to ten seconds.')
        require(type(transition_budget) is int and 1 <= transition_budget <= 1024,
                'RIG_STATE_BUDGET', 'Transition budget must be within 1..1024.')
        require(type(reduced_motion) is bool, 'RIG_STATE_REDUCED_MOTION', 'Reduced-motion mode must be boolean.')
        self.rig = rig
        self.mapping = map_states(rig, recipes=recipes, selected=selected)
        self.recipes = deepcopy(self.mapping['recipes'])
        self._require_state(initial)
        self.reduced_motion = reduced_motion
        self._static_poses = {}
        require(reduced_motion_poses is None or isinstance(reduced_motion_poses, dict),
                'RIG_STATE_REDUCED_MOTION', 'Reduced-motion alternatives must be a state-to-Pose mapping.')
        alternatives = reduced_motion_poses or {}
        require(set(alternatives) <= set(self.recipes), 'RIG_STATE_REDUCED_MOTION',
                'Reduced-motion alternatives must identify selected executable states.')
        if reduced_motion:
            require(set(alternatives) == set(self.recipes), 'RIG_STATE_REDUCED_MOTION',
                    'Every selected executable state requires an authored reduced-motion Pose.')
        for state, alternative in alternatives.items():
            validate_contract(alternative, 'Pose')
            identifier = self.rig.clips[self.recipes[state]['clips'][0]]['reduced_motion_pose']
            require(alternative['id'] == identifier and alternative['rig_version'] == rig.skeleton['version'],
                    'RIG_STATE_REDUCED_MOTION', 'Alternative must match the clip named reduced-motion pose and current skeleton version.')
            evaluate_deformation(rig, alternative)
            self._static_poses[state] = deepcopy(alternative)
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
                and math.isfinite(now) and now >= self.time,
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

    def _pose(self, now):
        if self.reduced_motion:
            return deepcopy(self._static_poses[self.state])
        recipe = self.recipes[self.state]
        elapsed = max(0., now-self.entered)
        clips = recipe['clips']
        duration = self._duration()
        looping = recipe['completion'] is None and self.rig.clips[clips[-1]]['loop']
        cycles = int(elapsed//duration) if looping else 0
        local = elapsed % duration if looping else min(elapsed, duration)
        cycle_delta = np.zeros(3)
        first_root = self._root(evaluate_animation(self.rig, self.rig.clips[clips[0]], 0)['pose'])
        for index, name in enumerate(clips):
            clip = self.rig.clips[name]
            start = evaluate_animation(self.rig, clip, 0)['pose']
            end = evaluate_animation(self.rig, clip, clip['duration'])['pose']
            delta = self._root(end)-self._root(start) if clip['root_motion'] == 'embedded' else np.zeros(3)
            if local <= clip['duration'] or index == len(clips)-1:
                pose = evaluate_animation(self.rig, clip, min(local, clip['duration']))['pose']
                prior_delta = cycle_delta.copy()
                break
            local -= clip['duration']
            cycle_delta += delta
        total_delta = sum((self._root(evaluate_animation(self.rig, self.rig.clips[c], self.rig.clips[c]['duration'])['pose'])
                           - self._root(evaluate_animation(self.rig, self.rig.clips[c], 0)['pose']) for c in clips if self.rig.clips[c]['root_motion'] == 'embedded'), np.zeros(3))
        root = self.rig.skeleton['root']
        with np.errstate(over='ignore', invalid='ignore'):
            translation = first_root+self._root(pose)-self._root(start)+self.root_offset+prior_delta+cycles*total_delta
        require(np.isfinite(translation).all(), 'RIG_STATE_TIME',
                'Requested runtime time exceeds finite root-motion representation.')
        pose['bones'].setdefault(root, {})['translation'] = translation.tolist()
        if self.source_pose is not None and self.blend_seconds and elapsed < self.blend_seconds:
            # Complete both poses: sparse target channels must fade old channels
            # back to rest, including morphs omitted from the target clip.
            source, target = deepcopy(self.source_pose), deepcopy(pose)
            for bone in self.rig.skeleton['bones']:
                for path, rest in bone['rest'].items():
                    target['bones'].setdefault(bone['id'], {}).setdefault(path, deepcopy(rest))
            for mesh in self.rig.geometry.meshes:
                for morph in mesh.morphs:
                    target['morphs'].setdefault(mesh.id, {}).setdefault(morph, mesh.morph_defaults.get(morph, 0.))
            pose = blend_poses(self.rig, source, target, elapsed/self.blend_seconds)
        return pose

    def _enter(self, state, now, pose):
        require(self.transition_count < self.transition_budget, 'RIG_STATE_BUDGET',
                'Runtime transition budget exhausted; create a new runtime explicitly.')
        self._require_state(state)
        first = self.rig.clips[self.recipes[state]['clips'][0]]
        start = evaluate_animation(self.rig, first, 0)['pose']
        offset = self._root(pose)-self._root(start)
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
        while not self.reduced_motion and self.recipes[self.state]['completion'] is not None and now >= self.entered+self._duration():
            target = self.recipes[self.state]['completion']
            if target not in self.recipes:
                status = 'completion-unavailable'
                break
            boundary = self.entered+self._duration()
            self._enter(target, boundary, self._pose(boundary))
        pose = self._pose(now)
        self.time = float(now)
        return dict(state=self.state, elapsed=now-self.entered, pose=pose,
                    status='held-reduced-motion' if self.reduced_motion else status, transition_count=self.transition_count,
                    pending_completion=self.recipes[self.state]['completion'] if status == 'completion-unavailable' else None)

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

    def dispatch(self, event, now):
        """Dispatch a selected catalog event or state alias through request policy."""
        require(isinstance(event, str) and event in self.mapping['events'], 'RIG_STATE_EVENT',
                'Event has no selected executable catalog state.')
        return self.request(self.mapping['events'][event], now)

    def recover(self, now):
        """Enter the selected catalog recovery state as an explicit override."""
        with self._transaction():
            self._check(now)
            pose = self._evaluate(now)['pose']
            target = self.recipes[self.state]['recovery']
            self._require_state(target)
            self._enter(target, now, pose)
            return self._evaluate(now)

    def cancel(self, now):
        """Cancel into executable static-neutral, preserving the current pose."""
        with self._transaction():
            self._check(now)
            self._require_state('static-neutral')
            pose = self._evaluate(now)['pose']
            self._enter('static-neutral', now, pose)
            return self._evaluate(now)
