"""Web UI context builders and Jinja2 template renderers for PeMS reports and dashboards."""

from pems.web.context import (
    generate_project_validation_web_page,
    generate_slices_html_report,
    launch_project_web_interface,
)

__all__ = [
    "generate_slices_html_report",
    "generate_project_validation_web_page",
    "launch_project_web_interface",
]
