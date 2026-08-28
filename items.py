"""Native QGIS anchor-node items with stable attached Bezier handles."""

import json
import math
import os

from qgis.PyQt.QtCore import QPointF, QRectF
from qgis.PyQt.QtGui import QBrush, QColor, QIcon, QPainterPathStroker, QPen, QPolygonF, QTransform
from qgis.PyQt.QtSvg import QSvgRenderer
from qgis.core import QgsGeometry, QgsLayoutItemPolygon, QgsLayoutItemPolyline, Qgis

from .spline import bezier_path, default_handles, handle_points, handles_from_explicit_nodes, sampled_bezier
from .recovery import enable_item_recovery, remove_item_snapshot, snapshot_item


SPLINE_POLYLINE_TYPE = 117342
SPLINE_POLYGON_TYPE = 117343
_ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")


def _prepare_render_context(context):
    render_context = context.renderContext()
    try:
        render_context.setFlag(Qgis.RenderContextFlag.DisableSymbolClippingToExtent, True)
    except (AttributeError, TypeError):
        # Older QGIS builds do not expose this flag; keep the native context.
        return render_context
    return render_context


class _BezierHandleMixin:
    _closed = False
    _handle_property = "layout_splines/bezier_handle_offsets_v3"
    _old_explicit_property = "layout_splines/explicit_bezier_handles_v2"

    def initializeBezierHandles(self):
        handles = self._ensure_handles(save=True)

        # During creation the controller may attach one scene-space drag vector
        # per inserted anchor. Convert those vectors only after QGIS has created
        # and positioned the real layout item, so snapping and QGIS' native node
        # normalization remain authoritative. A click leaves the default handle
        # unchanged; a click-drag creates a mirrored handle pair at that anchor.
        drag_vectors = getattr(self, "_layout_splines_creation_drag_vectors", None)
        if drag_vectors:
            nodes = list(self.nodes())
            changed = False
            for index, vector in enumerate(drag_vectors[:len(nodes)]):
                if vector is None:
                    continue
                anchor_scene = self.mapToScene(nodes[index])
                end_scene = QPointF(anchor_scene.x() + vector[0], anchor_scene.y() + vector[1])
                end_local = self.mapFromScene(end_scene)
                offset_x = end_local.x() - nodes[index].x()
                offset_y = end_local.y() - nodes[index].y()
                handles[index] = [-offset_x, -offset_y, offset_x, offset_y]
                changed = True
            if changed:
                self.prepareGeometryChange()
                self._bezier_handles = handles
                self._save_handles()
                self.update()
            try:
                del self._layout_splines_creation_drag_vectors
            except AttributeError:
                # A defensive fallback for wrappers which do not retain the
                # transient Python attribute after QGIS ownership transfer.
                return_value = None
                del return_value

        self._enable_recovery()

    def _enable_recovery(self):
        enable_item_recovery(self)

    def _sync_recovery_snapshot(self):
        snapshot_item(self)

    def cleanup(self):
        # cleanup() is called by QgsLayout when an item is intentionally
        # removed. Remove its recovery entry too, otherwise a deliberately
        # deleted spline could reappear on a later plugin-enabled open.
        remove_item_snapshot(self)
        return super().cleanup()

    def _ensure_handles(self, save=False):
        nodes = list(self.nodes())
        if self.customProperty(self._old_explicit_property, False):
            anchors, handles = handles_from_explicit_nodes(nodes, self._closed)
            self.setNodes(QPolygonF(anchors))
            self.setCustomProperty(self._old_explicit_property, False)
            self._bezier_handles = handles
            self._save_handles()
            return handles

        cached = getattr(self, "_bezier_handles", None)
        if cached is not None and len(cached) == len(nodes):
            return cached
        raw = self.customProperty(self._handle_property, "")
        try:
            handles = json.loads(str(raw)) if raw else []
            if len(handles) != len(nodes) or any(len(handle) != 4 for handle in handles):
                raise ValueError
            handles = [[float(value) for value in handle] for handle in handles]
        except (TypeError, ValueError, json.JSONDecodeError):
            handles = default_handles(nodes, self._closed)
            save = True
        self._bezier_handles = handles
        if save:
            self._save_handles()
        return handles

    def _save_handles(self):
        self.setCustomProperty(self._handle_property, json.dumps(self._bezier_handles, separators=(",", ":")))

    def setBezierHandle(self, anchor_index, kind, local_point, linked=True):
        handles = self._ensure_handles()
        nodes = list(self.nodes())
        if not (0 <= anchor_index < len(nodes)):
            return
        anchor = nodes[anchor_index]
        offset = [local_point.x() - anchor.x(), local_point.y() - anchor.y()]
        # Notify QGraphicsItem while boundingRect() still describes the old
        # handle positions. Qt can then invalidate both the old and new areas,
        # preventing trails when a control point is dragged.
        # QgsLayoutItem is a QGraphicsItem, so notify Qt before the control
        # geometry changes. This invalidates both the old and new extents.
        self.prepareGeometryChange()
        if kind == "in":
            handles[anchor_index][0:2] = offset
            if linked:
                handles[anchor_index][2:4] = [-offset[0], -offset[1]]
        else:
            handles[anchor_index][2:4] = offset
            if linked:
                handles[anchor_index][0:2] = [-offset[0], -offset[1]]
        self._bezier_handles = handles
        self._save_handles()
        self.update()

    def bezierHandleScenePoints(self):
        handles = self._ensure_handles()
        return [
            (index, kind, self.mapToScene(point))
            for index, kind, point in handle_points(self.nodes(), handles, self._closed)
        ]

    def readPropertiesFromElement(self, element, document, context):
        result = super().readPropertiesFromElement(element, document, context)
        if result:
            self._bezier_handles = None
            self._ensure_handles(save=True)
            self._enable_recovery()
        return result

    def _path(self):
        return bezier_path(self.nodes(), self._ensure_handles(), self._closed)

    def _curve(self):
        return sampled_bezier(self.nodes(), self._ensure_handles(), self._closed)

    def boundingRect(self):
        native = super().boundingRect()
        curve = self._path().controlPointRect()
        bleed = max(self.estimatedFrameBleed(), 0.4)
        curve.adjust(-bleed, -bleed, bleed, bleed)
        return native.united(curve)


class SplinePolyline(_BezierHandleMixin, QgsLayoutItemPolyline):
    def __init__(self, layout):
        super().__init__(layout)

    def type(self): return SPLINE_POLYLINE_TYPE
    def displayName(self): return self.id() or "<Spline Polyline>"
    def icon(self): return QIcon(os.path.join(_ICON_DIR, "spline_polyline_item.png"))

    def _draw(self, context, itemStyle=None):
        render_context = _prepare_render_context(context)
        curve = self._curve()
        if len(curve) < 2:
            return
        scale = render_context.convertToPainterUnits(1, Qgis.RenderUnit.Millimeters)
        mapped = QTransform.fromScale(scale, scale).map(curve)
        painter = render_context.painter()
        painter.save()
        try:
            symbol = self.symbol()
            symbol.startRender(render_context)
            try: symbol.renderPolyline(mapped, None, render_context)
            finally: symbol.stopRender(render_context)
        finally: painter.restore()
        painter.save()
        try:
            painter.scale(render_context.scaleFactor(), render_context.scaleFactor())
            self._draw_marker(painter, curve, True)
            self._draw_marker(painter, curve, False)
        finally: painter.restore()

    def _draw_marker(self, painter, curve, at_start):
        mode = self.startMarker() if at_start else self.endMarker()
        if mode == QgsLayoutItemPolyline.NoMarker: return
        point = curve[0] if at_start else curve[-1]
        neighbor = curve[1] if at_start else curve[-2]
        dx = neighbor.x() - point.x() if at_start else point.x() - neighbor.x()
        dy = neighbor.y() - point.y() if at_start else point.y() - neighbor.y()
        painter.save()
        try:
            painter.translate(point)
            painter.rotate(math.degrees(math.atan2(dy, dx)) + (180.0 if at_start else 0.0))
            width = self.arrowHeadWidth()
            if mode == QgsLayoutItemPolyline.ArrowHead:
                pen = QPen(self.arrowHeadStrokeColor()); pen.setWidthF(self.arrowHeadStrokeWidth())
                painter.setPen(pen); painter.setBrush(QBrush(self.arrowHeadFillColor()))
                painter.drawPolygon(QPolygonF([QPointF(0, 0), QPointF(-width, -width / 2), QPointF(-width, width / 2)]))
            elif mode == QgsLayoutItemPolyline.SvgMarker:
                marker = self.startSvgMarkerPath() if at_start else self.endSvgMarkerPath()
                renderer = QSvgRenderer(marker)
                if renderer.isValid():
                    view = renderer.viewBoxF(); height = width * view.height() / view.width() if view.width() else width
                    renderer.render(painter, QRectF(-width / 2, -height / 2, width, height))
        finally: painter.restore()

    def shape(self):
        stroker = QPainterPathStroker(); stroker.setWidth(max(2.0 * self.estimatedFrameBleed(), 0.8))
        return stroker.createStroke(self._path())


class SplinePolygon(_BezierHandleMixin, QgsLayoutItemPolygon):
    _closed = True

    def __init__(self, layout): super().__init__(layout)
    def type(self): return SPLINE_POLYGON_TYPE
    def displayName(self): return self.id() or "<Spline Polygon>"
    def icon(self): return QIcon(os.path.join(_ICON_DIR, "spline_polygon_item.png"))

    def _draw(self, context, itemStyle=None):
        render_context = _prepare_render_context(context)
        curve = self._curve()
        if len(curve) < 3: return
        scale = render_context.convertToPainterUnits(1, Qgis.RenderUnit.Millimeters)
        mapped = QTransform.fromScale(scale, scale).map(curve)
        painter = render_context.painter(); painter.save()
        try:
            symbol = self.symbol(); symbol.startRender(render_context)
            try: symbol.renderPolygon(mapped, None, None, render_context)
            finally: symbol.stopRender(render_context)
        finally: painter.restore()

    def shape(self): return self._path()

    def clipPath(self):
        polygon = self._path().toFillPolygon()
        return QgsGeometry.fromQPolygonF(QPolygonF([self.mapToScene(point) for point in polygon]))
