"""Selected catalog events, explicit recovery and authored still alternatives.

The scheduler remains owned by rig_state_runtime. This module adds no clock,
animation evaluator or event source; every public action uses explicit seconds.
"""
from copy import deepcopy
from rig_contracts import validate_contract
from rig_errors import require
from rig_skinning import evaluate_deformation


def compile_state_events(library, recipes):
    """Retain catalog events and index aliases for executable selected recipes."""
    events = {}
    for name, recipe in recipes.items():
        state = library.states[name]
        recipe['events'] = deepcopy(state['events'])
        for event in dict.fromkeys([*state['events'], 'state:'+name,
                                   *('state:'+alias for alias in state['aliases'])]):
            require(event not in events or events[event] == name, 'RIG_STATE_EVENT',
                    'Selected catalog event identifies conflicting states.')
            events[event] = name
    return events


class RigStateEvents:
    """Behavior mixed into the existing runtime, preserving its transactions."""

    def _configure_state_events(self, reduced_motion, reduced_motion_poses):
        require(type(reduced_motion) is bool, 'RIG_STATE_REDUCED_MOTION',
                'Reduced-motion mode must be boolean.')
        require(reduced_motion_poses is None or isinstance(reduced_motion_poses, dict),
                'RIG_STATE_REDUCED_MOTION',
                'Reduced-motion alternatives must be a state-to-Pose mapping.')
        alternatives = reduced_motion_poses or {}
        require(set(alternatives) <= set(self.recipes), 'RIG_STATE_REDUCED_MOTION',
                'Reduced-motion alternatives must identify selected executable states.')
        if reduced_motion:
            require(set(alternatives) == set(self.recipes), 'RIG_STATE_REDUCED_MOTION',
                    'Every selected executable state requires an authored reduced-motion Pose.')
        static_poses = {}
        for state, alternative in alternatives.items():
            validate_contract(alternative, 'Pose')
            identifier = self.rig.clips[self.recipes[state]['clips'][0]]['reduced_motion_pose']
            require(alternative['id'] == identifier and
                    alternative['rig_version'] == self.rig.skeleton['version'],
                    'RIG_STATE_REDUCED_MOTION',
                    'Alternative must match the clip named reduced-motion pose and current skeleton version.')
            evaluate_deformation(self.rig, alternative)
            static_poses[state] = deepcopy(alternative)
        self.reduced_motion = reduced_motion
        self._static_poses = static_poses

    def _held_state_pose(self):
        """Return an isolated authored alternative; never sample a 2D frame."""
        return deepcopy(self._static_poses[self.state])

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
