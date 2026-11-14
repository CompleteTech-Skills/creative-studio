"""Quantitative surface acceptance for the bounded pouring engineering fixture.

These are measurements of decoded native interfaces, not particle trajectories,
fluid mass, or a claim about leakage smaller than the declared grid envelope.
"""
import json
import math
from pathlib import Path

import physics_backends as core
from physics_liquid import _verify_cache_snapshot
from physics_liquid_example import build_liquid_inputs
from physics_liquid_mesh import canonical_vertices, decode_bobj


def clipped_surface(vertices, triangles, bounds):
    """Area of triangle surface clipped to an axis-aligned observation box."""
    total = []
    for triangle in triangles:
        polygon = [vertices[i] for i in triangle]
        for axis in range(3):
            for boundary, direction in ((bounds[0][axis], 1), (bounds[1][axis], -1)):
                clipped = []
                for a, b in zip(polygon, polygon[1:] + polygon[:1]):
                    da = direction * (a[axis] - boundary)
                    db = direction * (b[axis] - boundary)
                    if da >= 0:
                        clipped.append(a)
                    if (da >= 0) != (db >= 0):
                        t = da / (da - db)
                        clipped.append([a[k] + t * (b[k] - a[k]) for k in range(3)])
                polygon = clipped
        for i in range(1, len(polygon) - 1):
            a, b, c = polygon[0], polygon[i], polygon[i + 1]
            u = [b[k] - a[k] for k in range(3)]
            v = [c[k] - a[k] for k in range(3)]
            total.append(math.sqrt(sum(x*x for x in (
                u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0]))) / 2)
    return math.fsum(total)


def measure_surface(vertices, triangles, *, cell_size):
    # Canonical fixture: inner walls +/- .8, outer walls +/- .9,
    # floor bottom 0, floor top .1, rim 1.05. All lengths are metres.
    pool = [[-.8, .1-cell_size, -.8], [.8, 1.05-2*cell_size, .8]]
    low = [v for v in vertices if v[1] <= pool[1][1]]
    return dict(
        pool_surface_area_m2=clipped_surface(vertices, triangles, pool),
        pool_radial_extent_m=max((math.hypot(v[0], v[2]) for v in low), default=0.),
        lowest_surface_m=min((v[1] for v in vertices), default=None),
        # Count every excursion, including a splash above the rim. This is
        # conservative: legitimate overflow cannot be mislabelled as passing.
        exterior_excursion_m=max((max(abs(v[0])-.9, abs(v[2])-.9, -v[1], 0.) for v in vertices), default=0.),
    )


def assess_series(series, *, cell_size, emission_end_frame):
    """Pure assessment; synthetic inputs in unit tests never establish a bake."""
    low, high, open_ = (series[k] for k in ('baseline', 'viscous', 'uncontained'))
    if len({len(low), len(high), len(open_)}) != 1 or len(low) < 36:
        raise ValueError('Matched complete fixture samples required.')
    h = cell_size
    # One voxel for collision rasterization plus one for the rendered interface.
    leakage_envelope = 2*h
    # A 4x4-cell equivalent surface patch avoids a one-triangle positive result.
    minimum_patch = 16*h*h
    # Final quarter is after source shutoff and impact, and spans multiple outputs.
    late = list(range(3*len(low)//4, len(low)))
    impacts = [next((i for i, s in enumerate(samples)
                     if s['lowest_surface_m'] is not None and s['lowest_surface_m'] <= .1+h), None)
               for samples in (low, high)]
    impact = max(impacts) if None not in impacts else None
    window = list(range(impact+1, impact+7)) if impact is not None else []
    valid_window = bool(window) and window[-1] < late[0] and window[0] > emission_end_frame
    differences = [low[i]['pool_radial_extent_m']-high[i]['pool_radial_extent_m'] for i in window] if valid_window else []
    mean_difference = math.fsum(differences)/len(differences) if differences else None
    # Two independently meshed radial interfaces each receive one cell of
    # ambiguity. A hash change, a sub-cell effect, or a reversed effect fails.
    viscosity_threshold = 2*h
    surfaces_present = valid_window and all(
        samples[i]['pool_surface_area_m2'] >= minimum_patch
        for samples in (low, high) for i in window)
    checks = dict(
        contained_leakage_envelope=max(s['exterior_excursion_m'] for samples in (low, high) for s in samples) <= leakage_envelope,
        initial_pool_absent=max(s['pool_surface_area_m2'] for s in low[:min(6, len(low))]) <= h*h,
        accumulated_surface_retained=min(low[i]['pool_surface_area_m2'] for i in late) >= minimum_patch,
        viscous_surface_retained=min(high[i]['pool_surface_area_m2'] for i in late) >= minimum_patch,
        uncontained_control_drains=max(open_[i]['pool_surface_area_m2'] for i in late) <= h*h,
        viscosity_surfaces_present=surfaces_present,
        viscosity_reduces_impact_spread=(valid_window and mean_difference > viscosity_threshold
                                       and min(differences) > 0 and surfaces_present),
    )
    return dict(status='passed' if all(checks.values()) else 'failed', checks=checks,
        cell_size_m=h, leakage_envelope_m=leakage_envelope,
        minimum_retained_surface_m2=minimum_patch, late_frames=late,
        impact_frames=impacts, viscosity_frames=window,
        viscosity_spread_reduction_m=differences, mean_viscosity_spread_reduction_m=mean_difference,
        viscosity_comparison_tolerance_m=viscosity_threshold,
        maximum_exterior_excursion_m=max(s['exterior_excursion_m'] for samples in (low, high) for s in samples),
        minimum_late_contained_surface_m2=min(low[i]['pool_surface_area_m2'] for i in late),
        minimum_late_viscous_surface_m2=min(high[i]['pool_surface_area_m2'] for i in late),
        maximum_late_uncontained_surface_m2=max(open_[i]['pool_surface_area_m2'] for i in late))


def evaluate_caches(caches):
    """Verify integrity, enforce matched inputs, then decode every native mesh."""
    if set(caches) != {'baseline', 'viscous', 'uncontained'}:
        raise ValueError('Exactly baseline, viscous, and uncontained caches required.')
    series = {}; identities = {}
    for case, directory in caches.items():
        root = Path(directory)
        verified, payloads = _verify_cache_snapshot(root)
        world = core.parse(payloads['world.json'])
        settings = core.parse(payloads['liquid-settings.json'])
        # This acceptance profile is deliberately specific: arbitrary sources,
        # timing, radii, obstacles, and settings require a different experiment.
        expected, inputs, expected_settings = build_liquid_inputs(
            viscosity=.01 if case == 'viscous' else 0., container=case != 'uncontained')
        # A closed domain floor coincident with the container floor could mask
        # container leakage. Keep it OPEN in both contained cases and control.
        expected_settings['boundaries']['bottom'] = False
        if world != expected or settings != expected_settings or any(
                payloads['inputs/'+name] != payload for name, payload in inputs.items()):
            raise ValueError('Cache is not the matched 48-frame resolution-24 engineering control: '+case)
        domain = world['components'][0]
        series[case] = []
        h = domain['size'][0]/domain['resolution'][0]
        for frame in range(world['frame_range']['end']):
            payload = payloads[f'cache/mesh/fluid_mesh_{frame+1:04d}.bobj.gz']
            mesh = decode_bobj(payload)
            series[case].append(dict(frame=frame, mesh_sha256=core.digest(payload), **measure_surface(
                canonical_vertices(mesh, domain), mesh['triangles'], cell_size=h)))
        identities[case] = dict(cache=str(root.resolve()), manifest_sha256=core.digest(payloads['liquid-cache.json']),
            worker_sha256=core.digest(payloads['worker.py']),
            world_sha256=verified['world_sha256'], backend=verified['backend'], resources=verified['resources'],
            settings=settings, viscosity_m2_per_s=domain['viscosity'])
        # Do not retain this cache while the next bounded snapshot is loaded.
        del payloads, payload, mesh
    result = assess_series(series, cell_size=h, emission_end_frame=expected_settings['emission_end_frame'])
    return dict(protocol=1, experiment='pouring-48-resolution24-v1', assessment=result,
        assessor_sha256=core.digest(Path(__file__).read_bytes()),
        representation='independently decoded native BOBJ triangle surface in canonical metres',
        limitations=['surface geometry proxies, not exact fluid mass or mass conservation',
                     'no guarantee for leakage smaller than the two-cell envelope',
                     'bounded engineering fixture only; no secondary particles or two-way coupling'],
        cases=identities, samples=series)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    for case in ('baseline', 'viscous', 'uncontained'):
        parser.add_argument('--'+case, required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = evaluate_caches({k: getattr(args, k) for k in ('baseline', 'viscous', 'uncontained')})
    # Preserve failed physical results too; never publish a success-only receipt.
    with Path(args.output).open('x') as output:
        json.dump(report, output, indent=2, sort_keys=True, allow_nan=False)
        output.write('\n')
    return 0 if report['assessment']['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
