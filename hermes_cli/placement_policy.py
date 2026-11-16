"""placement_policy -- DEPRECATED alias of ``hermes_cli.allocation_policy`` (rename wave 3, t_7be48220,
t_fc257547). Remove after one full day with zero ``module:hermes_cli.placement_policy`` lines in
var/ci-box/deprecated-name-reads.log.

The import binds the SAME module object as ``allocation_policy`` (sys.modules alias), so monkeypatching and
isinstance checks behave exactly as through the new name.
"""
import sys as _sys

from hermes_cli import allocation_policy as _ap

_ap.count_deprecated_read("module:hermes_cli.placement_policy")
_sys.modules[__name__] = _ap
