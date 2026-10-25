"""Quarantined legacy modules.

Nothing in the live pipeline imports this package. These modules contain
Ctrl+A / Ctrl+C clipboard mirroring paths that read and replace whole editor
buffers; they are kept only for reference and their isolated unit tests.
Live delivery goes through editor_bridge / editor_adapters instead.
"""
