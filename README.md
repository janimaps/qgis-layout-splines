# Layout Splines

Layout Splines adds two node-based items to every QGIS Layout Designer:

- **Spline Polygon**
- **Spline Polyline**

The items use real cubic Bezier control handles. Select QGIS's **Edit Nodes Item** tool and select the spline. QGIS displays all editable nodes; every anchor is connected to its incoming and outgoing control nodes by blue dashed guide lines. Paired handles move symmetrically by default. Hold **Ctrl** on Windows/Linux or **Command** on macOS while dragging to move one handle independently.

## Compatibility

- QGIS 3.42 (Qt 5 / PyQt 5)
- QGIS 4.x (Qt 6 / PyQt 6)

The plugin imports Qt exclusively through `qgis.PyQt` and uses the layout registry and node-item APIs shared by both versions.

## Install

1. In QGIS, choose **Plugins > Manage and Install Plugins**.
2. Open **Install from ZIP**.
3. Select `qgis_layout_splines-1.0.0.zip` and install it.
4. Open or create a Print Layout.
5. Open the **Add Node Item** drop-down. **Spline Polygon** and **Spline Polyline** appear in that order.
6. Click to add a normal anchor, or click-drag to add an anchor with a mirrored Bezier handle pair. Right-click finishes the item. Backspace removes the last inserted anchor (and cancels when only the first anchor remains), matching QGIS node-item creation behavior.
7. Select **Edit Nodes Item**. Hovering an anchor or Bezier handle endpoint highlights that target only; hover does not select or move geometry. Drag a circular control handle to edit it. Its paired handle mirrors the movement unless **Ctrl** (Windows/Linux) or **Command** (macOS) is held.

## Native behavior retained

The classes inherit QGIS's native `QgsLayoutItemPolyline` and `QgsLayoutItemPolygon`. Accordingly, they retain standard node editing, symbols, item properties, undo/redo, position and size, rotation, locking, grouping, copy/paste, templates, project persistence, exports, and polygon clipping-source support.

## Curve model

Adjacent anchors are joined by cubic Bezier segments. Incoming and outgoing handles are stored as offsets attached to each anchor, so moving a handle never changes the QGIS item rectangle or inserts a false line vertex. Initial handles use the uniform Catmull-Rom conversion (`1/6` tangent rule). Dragging a handle mirrors its offset onto its pair by default; holding the platform modifier allows independent movement. Control guides appear only in the interactive Layout Designer preview and are excluded from exports.

## Unloading

QGIS layout registries do not expose an unregister API. If the plugin is disabled during a session, its already-registered item metadata remains until QGIS is restarted. Existing project items require the plugin to be enabled when the project is loaded.

## License

GNU General Public License v2 or later.

## Missing-plugin recovery

Each spline stores an exact recovery snapshot in the owning layout's custom
properties. QGIS serializes these layout properties using its native layout XML,
so they remain in the project even if the plugin is unavailable and the project
is subsequently saved. No native fallback polygon/polyline is created: without
Layout Splines the preserved items are not rendered and cannot be edited with
QGIS's normal node tools. When the plugin is available again and the Layout
Designer is opened, missing spline items are reconstructed automatically from
the preserved snapshots.
