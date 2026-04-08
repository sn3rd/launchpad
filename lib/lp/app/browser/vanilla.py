# Copyright 2026 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Reusable Vanilla framework helpers for browser views."""

__all__ = [
    "Tabs",
]


import html as html_module
from typing import List, Literal, Tuple
from urllib.parse import parse_qs, urlencode


class Tabs:
    """Server-side helper for the Vanilla tabs pattern.

    https://vanillaframework.io/docs/patterns/tabs

    Renders the ``p-tabs`` navigation via :attr:`render` **without** ARIA
    tab roles — ``tabs.js`` hydrates the markup with ``role="tablist"``,
    ``role="tab"``, ``aria-selected``, and ``aria-controls`` when JS is
    available.  In no-JS mode, the active tab carries
    ``aria-current="page"`` and tabs act as plain navigation links.

    TAL usage::

        <tal:tabs tal:replace="structure view/my_tabs/render" />
        <div tal:attributes="id view/my_tabs/active_panel_id"
             tal:condition="python:view.my_tabs.active == 'key'">
          …panel content for 'key'…
        </div>
    """

    def __init__(
        self,
        param: str,
        default: str,
        tabs: List[Tuple[str, str]],
        request,
        base_url: str,
        swap_url: str,
        swap_target: str,
        swap_style: Literal["innerHTML", "outerHTML"],
        aria_label: str,
    ) -> None:
        self._param = param
        self._default = default
        self._tabs = tabs
        self._valid_keys = {k for k, _ in tabs}
        self._request = request
        self._base_url = base_url
        self._swap_url = swap_url
        self._swap_target = swap_target
        self._swap_style = swap_style
        self.aria_label = aria_label

    @property
    def active(self) -> str:
        """Return the key of the currently selected tab."""
        key = self._request.form.get(self._param, self._default)
        return key if key in self._valid_keys else self._default

    @property
    def active_panel_id(self) -> str:
        """Return the panel ID for the currently selected tab."""
        return "%s-%s-panel" % (self._param, self.active)

    def _build_url(self, base_url: str, key: str) -> str:
        """Build a URL preserving existing query params."""
        params = parse_qs(
            self._request.get("QUERY_STRING", ""),
            keep_blank_values=True,
        )
        if key != self._default:
            params[self._param] = [key]
        else:
            params.pop(self._param, None)
        qs = urlencode(params, doseq=True)
        return base_url + "?" + qs if qs else base_url

    def __iter__(self):
        selected = self.active
        for key, label in self._tabs:
            yield {
                "label": label,
                "href": self._build_url(self._base_url, key),
                "swap_url": self._swap_url,
                "swap_param_key": self._param,
                "swap_param_value": key,
                "is_default": key == self._default,
                "active": "true" if selected == key else "false",
                "panel_id": "%s-%s-panel" % (self._param, key),
            }

    @property
    def render(self) -> str:
        """Render the p-tabs navigation as an HTML string.

        The server-rendered markup intentionally omits ARIA tab roles
        (``role="tablist"``, ``role="tab"``, ``aria-selected``).  These
        are added by ``tabs.js`` on hydration so the tab contract is
        only active when JS can fulfil it.

        Instead, the active tab gets ``aria-current="page"`` (suitable
        for the no-JS page-navigation fallback) and each link carries a
        ``data-controls`` attribute that JS reads to set ``aria-controls``.
        """
        esc = html_module.escape
        items = []
        for tab in self:
            aria_current = (
                ' aria-current="page"' if tab["active"] == "true" else ""
            )
            swap_default = " swap-default" if tab["is_default"] else ""
            swap_current = " swap-current" if tab["active"] == "true" else ""
            items.append(
                '<div class="p-tabs__item">'
                '<a class="p-tabs__link"'
                ' href="%s"'
                ' swap-url="%s"'
                ' swap-target="%s"'
                ' swap-style="%s"'
                ' swap-param-key="%s"'
                ' swap-param-value="%s"'
                "%s"
                "%s"
                "%s"
                ' data-controls="%s">%s</a>'
                "</div>"
                % (
                    esc(tab["href"]),
                    esc(tab["swap_url"]),
                    esc(self._swap_target),
                    esc(self._swap_style),
                    esc(tab["swap_param_key"]),
                    esc(tab["swap_param_value"]),
                    swap_default,
                    swap_current,
                    aria_current,
                    esc(tab["panel_id"]),
                    esc(tab["label"]),
                ),
            )
        return (
            '<div class="p-tabs">'
            '<div class="p-tabs__list" data-js="tabs"'
            ' aria-label="%s">%s</div>'
            "</div>"
            % (
                esc(self.aria_label),
                "".join(items),
            )
        )
