"""Plugin-independent persistence for custom spline layout items.

QGIS cannot instantiate unknown custom layout item types when the plugin which
registered them is absent.  To prevent a later save from permanently dropping
those items, every spline also keeps an exact XML snapshot in a *layout custom
property*.  QGIS understands and preserves layout custom properties without
this plugin, so the snapshot survives while remaining completely non-rendered
and non-editable.
"""

import json

from qgis.PyQt.QtXml import QDomDocument
from qgis.core import QgsReadWriteContext


RECOVERY_PROPERTY = "layout_splines/recovery_manifest_v1"


def _read_manifest(layout):
    if layout is None:
        return {}
    raw = layout.customProperty(RECOVERY_PROPERTY, "")
    if not raw:
        return {}
    try:
        manifest = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _write_manifest(layout, manifest):
    if layout is None:
        return
    if manifest:
        layout.setCustomProperty(
            RECOVERY_PROPERTY,
            json.dumps(manifest, separators=(",", ":"), sort_keys=True),
        )
    else:
        layout.removeCustomProperty(RECOVERY_PROPERTY)


def _context_for_layout(layout):
    context = QgsReadWriteContext()
    try:
        project = layout.project()
        if project is not None:
            context.setPathResolver(project.pathResolver())
    except (AttributeError, RuntimeError, TypeError):
        # A plain context is sufficient for spline geometry and normal symbols;
        # path resolution is only an enhancement for path-backed symbol assets.
        return context
    return context


def snapshot_item(item):
    """Persist an exact QgsLayoutItem XML snapshot in the owning layout."""
    if item is None:
        return
    try:
        layout = item.layout()
        uuid = str(item.uuid())
    except (AttributeError, RuntimeError, TypeError):
        return
    if layout is None or not uuid:
        return

    document = QDomDocument("LayoutSplinesRecovery")
    root = document.createElement("LayoutSplinesRecovery")
    document.appendChild(root)
    try:
        if not item.writeXml(root, document, _context_for_layout(layout)):
            return
    except (AttributeError, RuntimeError, TypeError):
        return

    element = root.firstChildElement("LayoutItem")
    if element.isNull():
        return

    manifest = _read_manifest(layout)
    manifest[uuid] = {
        "type": int(item.type()),
        "xml": element.ownerDocument().toString(-1),
    }
    _write_manifest(layout, manifest)


def remove_item_snapshot(item):
    """Forget recovery state when a spline is intentionally deleted."""
    if item is None:
        return
    try:
        layout = item.layout()
        uuid = str(item.uuid())
    except (AttributeError, RuntimeError, TypeError):
        return
    if layout is None or not uuid:
        return
    manifest = _read_manifest(layout)
    if uuid in manifest:
        del manifest[uuid]
        _write_manifest(layout, manifest)


def enable_item_recovery(item):
    """Connect a live spline to automatic recovery-snapshot maintenance."""
    if item is None or getattr(item, "_layout_splines_recovery_enabled", False):
        return
    item._layout_splines_recovery_enabled = True
    try:
        item.changed.connect(item._sync_recovery_snapshot)
    except (AttributeError, RuntimeError, TypeError):
        item._layout_splines_recovery_enabled = False
        return
    snapshot_item(item)


def recover_layout(layout, item_classes):
    """Restore custom spline items missing from a layout.

    ``item_classes`` maps custom type IDs to their Python classes. Existing
    items are never duplicated. Recovery payloads are intentionally retained
    after restoration so the project remains protected if it is subsequently
    opened without the plugin again.
    """
    if layout is None:
        return []

    manifest = _read_manifest(layout)
    restored = []
    context = _context_for_layout(layout)

    for uuid, record in list(manifest.items()):
        if not isinstance(record, dict):
            continue
        try:
            if layout.itemByUuid(uuid) is not None:
                continue
        except (AttributeError, RuntimeError, TypeError):
            return restored

        try:
            type_id = int(record.get("type"))
        except (TypeError, ValueError):
            continue
        item_class = item_classes.get(type_id)
        if item_class is None:
            continue

        xml = record.get("xml", "")
        if not isinstance(xml, str) or not xml:
            continue
        document = QDomDocument("LayoutSplinesRecovery")
        if not document.setContent(xml):
            continue
        element = document.documentElement().firstChildElement("LayoutItem")
        if element.isNull():
            # Older snapshots may contain the LayoutItem as document root.
            root = document.documentElement()
            if root.tagName() == "LayoutItem":
                element = root
        if element.isNull():
            continue

        item = item_class(layout)
        try:
            if not item.readXml(element, document, context):
                item.deleteLater()
                continue
            layout.addLayoutItem(item)
            item.finalizeRestoreFromXml()
            if hasattr(item, "_enable_recovery"):
                item._enable_recovery()
            restored.append(item)
        except (AttributeError, RuntimeError, TypeError):
            try:
                item.deleteLater()
            except RuntimeError:
                item = None
            continue

    return restored
