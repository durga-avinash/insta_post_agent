from importlib import import_module


def test_daily_generation_cli_entrypoint_is_importable():
    module = import_module("insta_post_agent.cli")
    assert callable(module.main)
