import json
import re
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).parent.parent
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def test_release_please_configuration_is_bootstrapped_consistently():
    config = json.loads((REPOSITORY_ROOT / "release-please-config.json").read_text())
    manifest = json.loads((REPOSITORY_ROOT / ".release-please-manifest.json").read_text())
    version = (REPOSITORY_ROOT / "version.txt").read_text().strip()

    assert config["release-type"] == "simple"
    assert config["include-v-in-tag"] is True
    assert config["include-component-in-tag"] is False
    assert config["packages"]["."]["package-name"] == "pihole6_exporter_with_queries_time"
    assert re.fullmatch(r"[0-9a-f]{40}", config["bootstrap-sha"])
    assert manifest == {".": version}
    assert SEMVER.fullmatch(version)


def test_release_workflow_publishes_only_semver_releases():
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "docker.yml").read_text()

    assert "googleapis/release-please-action@v5" in workflow
    assert workflow.count("type=semver,pattern={{version}}") == 2
    assert workflow.count("type=semver,pattern={{major}}.{{minor}}") == 2
    assert workflow.count("type=semver,pattern={{major}}") == 4
    assert workflow.count("type=raw,value=latest") == 2
    assert workflow.count("type=sha,prefix=sha-") == 2
    assert "type=ref,event=tag" not in workflow
    assert "steps.release.outputs.release_created == 'true'" in workflow
