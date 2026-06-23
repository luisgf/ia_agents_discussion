# Copyright (C) 2025 Luis González Fernández
# SPDX-License-Identifier: GPL-3.0-or-later

"""Technical diagnosis debate agents."""

__all__ = ["__version__"]

# Single source of truth for the version — pyproject.toml reads it via
# [tool.hatch.version]. Bump it following SemVer (see CODING_STYLE.md) when
# cutting a release, then tag v<version> to trigger the release workflow.
__version__ = "0.1.0"
