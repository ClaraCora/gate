from __future__ import annotations

from gate.config import load_settings
from gate.haproxy_config import render_haproxy_config


def test_renders_every_configured_entry() -> None:
    settings = load_settings()
    rendered = render_haproxy_config(settings)

    for region in settings.regions:
        assert f"frontend gate_{region.id}" in rendered
        assert f"bind 127.0.0.1:{region.socks_port}" in rendered
        assert f"backend gate_{region.id}_slots" in rendered
        assert f"server {region.id}-a " in rendered
        assert f"server {region.id}-b " in rendered
