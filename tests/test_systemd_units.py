from pathlib import Path


REPOSITORY_ROOT = Path(__file__).parent.parent
SERVICE_FILES = (
    REPOSITORY_ROOT / "metrics_exporter" / "pihole6_metrics_exporter.service",
    REPOSITORY_ROOT / "logs_exporter" / "pihole6_logs_exporter.service",
)


def test_systemd_services_do_not_expose_api_token_in_process_arguments():
    for service_file in SERVICE_FILES:
        unit = service_file.read_text()

        assert "EnvironmentFile=-/etc/pihole6_exporter/pihole6_exporter.env" in unit
        assert "${PIHOLE_API_TOKEN}" not in unit
        assert " -k " not in unit
