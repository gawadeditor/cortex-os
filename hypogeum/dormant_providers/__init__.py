"""
hypogeum/dormant_providers/ — quarantined official/direct provider
clients (plan §5.5/§6 Commit K).

Contains 5 network-capable modules with no production caller anywhere
in the pipeline: byteplus_ark.py, higgsfield.py, veo_official.py,
runway_official.py, kling_official.py. They are preserved, not
deleted -- hypogeum/provider_verifier.py's own credential probes still
lazily import 4 of the 5 (byteplus_ark, runway_official, kling_official,
veo_official; higgsfield.py has no importer at all, dead even by that
measure) to confirm an API key is valid without ever generating real
media. That is the ONLY sanctioned production import of anything in
this package -- see tests/test_provider_boundary_inventory.py::
TestDeadUnreachableClientsQuarantineStatus, which asserts this
exhaustively across the whole repository, not just this directory.
"""
