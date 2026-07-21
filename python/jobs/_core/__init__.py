"""Job-agnostic core: dataclasses, the Aspect protocol, the cached client,
and the actor/fight finders. Nothing in here may import from a per-job
package — dependency arrows point _outward_ from _core to per-job modules.
"""
