# Sleep Classifier — pytest suite.
#
# One ``tests/test_<module>.py`` per ``src/<module>.py``; cross-module
# orchestrator tests use the ``test_smart_sleep_service_*`` prefix.
# pytest-asyncio is configured as ``asyncio_mode = "auto"`` in
# ``pyproject.toml``, so plain ``async def test_*`` works without any
# decorator.
