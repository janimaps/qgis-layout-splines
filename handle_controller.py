"""Mouse interaction and scene overlays for editable Bezier handles."""

import math
import sys
import weakref

from qgis.PyQt.QtCore import QEvent, QLineF, QObject, QPoint, QPointF, QRectF, Qt, QTimer
from qgis.PyQt.QtGui import QBrush, QColor, QPainterPath, QPen
from qgis.PyQt.QtWidgets import QApplication, QGraphicsEllipseItem, QGraphicsItem, QGraphicsLineItem, QGraphicsPathItem, QGraphicsRectItem
from qgis.gui import QgsGui, QgsLayoutViewToolAddNodeItem, QgsLayoutViewToolEditNodes

from .items import SPLINE_POLYGON_TYPE, SPLINE_POLYLINE_TYPE


_CONTROLLERS_BY_VIEW = {}


def notify_creation_started(view, item_type):
    """Called by spline GUI metadata when QGIS starts our node-item creation.

    This is more reliable than introspecting QgsLayoutViewToolAddNodeItem from
    Python, because SIP may expose the native tool through a base wrapper.
    """
    ref = _CONTROLLERS_BY_VIEW.get(id(view))
    controller = ref() if ref is not None else None
    if controller is not None:
        controller._start_creation_session(item_type)


def _event_type(name):
    try:
        return getattr(QEvent.Type, name)
    except AttributeError:
        return getattr(QEvent, name)


def _left_button():
    try:
        return Qt.MouseButton.LeftButton
    except AttributeError:
        return Qt.LeftButton


def _no_button():
    try:
        return Qt.MouseButton.NoButton
    except AttributeError:
        return Qt.NoButton


def _independent_handle_modifier():
    name = "MetaModifier" if sys.platform == "darwin" else "ControlModifier"
    try:
        return getattr(Qt.KeyboardModifier, name)
    except AttributeError:
        return getattr(Qt, name)


def _dash_line():
    try:
        return Qt.PenStyle.DashLine
    except AttributeError:
        return Qt.DashLine


def _dot_line():
    try:
        return Qt.PenStyle.DotLine
    except AttributeError:
        return Qt.DotLine


def _no_brush():
    try:
        return Qt.BrushStyle.NoBrush
    except AttributeError:
        return Qt.NoBrush


def _ignore_transformations_flag():
    name = "ItemIgnoresTransformations"
    try:
        return getattr(QGraphicsItem.GraphicsItemFlag, name)
    except AttributeError:
        return getattr(QGraphicsItem, name)


def _scene_rect_from_local(item, local_rect):
    """Map a local item rectangle to a normalized scene rectangle."""
    points = [
        QPointF(local_rect.left(), local_rect.top()),
        QPointF(local_rect.right(), local_rect.top()),
        QPointF(local_rect.right(), local_rect.bottom()),
        QPointF(local_rect.left(), local_rect.bottom()),
    ]
    scene_points = [item.mapToScene(point) for point in points]
    rect = QRectF(scene_points[0], scene_points[0])
    for point in scene_points[1:]:
        rect = rect.united(QRectF(point, point))
    return rect.normalized()


def _is_preview(layout):
    try:
        return layout is not None and layout.renderContext().isPreviewRender()
    except (AttributeError, RuntimeError, TypeError):
        # A paint callback must never propagate an exception into Qt. If a
        # layout is being replaced or destroyed, omit the transient controls.
        return False


class _GuideOverlayItem(QGraphicsItem):
    """Single scene overlay for all Bezier guide lines.

    Using one graphics item avoids the show/hide/setLine churn from a separate
    QGraphicsLineItem per handle. That churn can race QGIS' native Edit Nodes
    hover repaint and briefly leave guides using geometry from different paint
    passes. The overlay stores one coherent snapshot and paints it atomically.
    """

    def __init__(self, layout):
        super().__init__()
        self._layout = layout
        self._lines = []
        self._bounds = QRectF()
        self.setZValue(1.0e9)
        self.setAcceptedMouseButtons(_no_button())

        self._pen = QPen(_GUIDE_GREEN)
        self._pen.setWidthF(1.5)
        self._pen.setCosmetic(True)
        self._pen.setStyle(_dash_line())

    def boundingRect(self):
        return self._bounds

    def setLines(self, lines):
        new_lines = [QLineF(line) for line in lines]
        if new_lines:
            bounds = QRectF(new_lines[0].p1(), new_lines[0].p2()).normalized()
            for line in new_lines[1:]:
                bounds = bounds.united(QRectF(line.p1(), line.p2()).normalized())
            # Cosmetic pen width is in device pixels, but a small scene margin
            # is enough to prevent Qt clipping at guide endpoints.
            bounds.adjust(-2.0, -2.0, 2.0, 2.0)
        else:
            bounds = QRectF()

        if bounds != self._bounds:
            self.prepareGeometryChange()
            self._bounds = bounds
        self._lines = new_lines
        self.setVisible(bool(new_lines))
        self.update()

    def paint(self, painter, option, widget=None):
        if not _is_preview(self._layout) or not self._lines:
            return
        painter.save()
        try:
            painter.setPen(self._pen)
            painter.setBrush(Qt.BrushStyle.NoBrush if hasattr(Qt, "BrushStyle") else Qt.NoBrush)
            painter.drawLines(self._lines)
        finally:
            painter.restore()




class _CreationPreviewItem(QGraphicsPathItem):
    """Native-looking cubic preview while a spline is being created.

    QGIS still owns snapping, insertion, Backspace and right-click completion.
    This item only paints the spline preview: committed sections are solid red,
    the temporary/uncommitted section is dotted red, and committed polygon
    geometry receives a translucent red fill.
    """

    def __init__(self, layout):
        super().__init__()
        self._layout = layout
        self._committed_path = QPainterPath()
        self._live_path = QPainterPath()
        self._fill_path = QPainterPath()

        self._committed_pen = QPen(QColor(255, 45, 45, 235))
        self._committed_pen.setWidthF(1.25)
        self._committed_pen.setCosmetic(True)

        self._live_pen = QPen(QColor(255, 45, 45, 235))
        self._live_pen.setWidthF(1.25)
        self._live_pen.setCosmetic(True)
        self._live_pen.setStyle(_dot_line())

        self._fill_brush = QBrush(QColor(255, 45, 45, 30))
        # QGraphicsPathItem's own pen/brush are not used; set them transparent
        # so only our custom paint routine is visible.
        self.setPen(QPen(Qt.PenStyle.NoPen if hasattr(Qt, "PenStyle") else Qt.NoPen))
        self.setBrush(QBrush(_no_brush()))
        self.setZValue(1.0e9 - 1.0)
        self.setAcceptedMouseButtons(_no_button())
        self.hide()

    def setPreviewPaths(self, committed_path, live_path, fill_path):
        self._committed_path = QPainterPath(committed_path)
        self._live_path = QPainterPath(live_path)
        self._fill_path = QPainterPath(fill_path)

        # Keep QGraphicsPathItem's geometry/bounds equal to the union of all
        # painted paths so scene invalidation remains correct.
        combined = QPainterPath(committed_path)
        combined.addPath(live_path)
        combined.addPath(fill_path)
        super().setPath(combined)
        self.update()

    def paint(self, painter, option, widget=None):
        if not _is_preview(self._layout):
            return
        painter.save()
        try:
            if not self._fill_path.isEmpty():
                painter.setPen(QPen(Qt.PenStyle.NoPen if hasattr(Qt, "PenStyle") else Qt.NoPen))
                painter.setBrush(self._fill_brush)
                painter.drawPath(self._fill_path)
            if not self._committed_path.isEmpty():
                painter.setPen(self._committed_pen)
                painter.setBrush(_no_brush())
                painter.drawPath(self._committed_path)
            if not self._live_path.isEmpty():
                painter.setPen(self._live_pen)
                painter.setBrush(_no_brush())
                painter.drawPath(self._live_path)
        finally:
            painter.restore()


class _SelectionBoundsOverlayItem(QGraphicsItem):
    """Non-interactive selection bounds drawn from the visible spline extent.

    QGIS' native mouse-handle frame for QgsLayoutNodesItem is derived from the
    anchor-node item rectangle. This overlay paints a QGIS-like frame around the
    rendered Bezier curve extent while leaving the native mouse-handle item
    alive and transparent for selection/move hit testing. Because it is a
    separate scene item, it can be removed immediately on deselection without
    leaving stale item-painted corner marks.
    """

    def __init__(self, layout):
        super().__init__()
        self._layout = layout
        self._rect = QRectF()
        self.setZValue(1.0e9 + 1.0)
        self.setAcceptedMouseButtons(_no_button())
        self._pen = QPen(QColor(0, 145, 255, 235))
        self._pen.setWidthF(1.0)
        self._pen.setCosmetic(True)
        self._handle_pen = QPen(QColor(0, 145, 255, 235))
        self._handle_pen.setWidthF(1.0)
        self._handle_pen.setCosmetic(True)
        self._handle_brush = QBrush(QColor(255, 255, 255, 255))
        self.hide()

    def boundingRect(self):
        if self._rect.isNull():
            return QRectF()
        rect = QRectF(self._rect)
        rect.adjust(-7.0, -7.0, 7.0, 7.0)
        return rect

    def setSceneRect(self, rect):
        new_rect = QRectF(rect).normalized() if rect is not None else QRectF()
        if new_rect != self._rect:
            self.prepareGeometryChange()
            self._rect = new_rect
        self.setVisible(not new_rect.isNull() and new_rect.isValid())
        self.update()

    def paint(self, painter, option, widget=None):
        # This item is a GUI-only selection decoration owned by the layout view
        # controller. Paint it whenever it is visible in the scene; exports use
        # the layout renderer rather than this transient view overlay. Avoid an
        # isPreviewRender() guard here because some QGIS builds report false
        # while painting cosmetic view overlays in Select/Move mode.
        if self._rect.isNull():
            return
        painter.save()
        try:
            painter.setPen(self._pen)
            painter.setBrush(_no_brush())
            painter.drawRect(self._rect)
            painter.setPen(self._handle_pen)
            painter.setBrush(self._handle_brush)
            # Match QGIS' default selection grip feel: handles should be
            # screen-pixel sized, not layout-unit sized.  The overlay is drawn
            # in scene coordinates, so convert a small pixel size through the
            # active painter transform.
            transform = painter.worldTransform()
            scale_x = math.hypot(transform.m11(), transform.m12())
            scale_y = math.hypot(transform.m22(), transform.m21())
            scale = max(scale_x, scale_y, 1.0e-9)
            size = 6.0 / scale
            half = size / 2.0
            x0, x1 = self._rect.left(), self._rect.right()
            y0, y1 = self._rect.top(), self._rect.bottom()
            xm, ym = self._rect.center().x(), self._rect.center().y()
            for point in (
                QPointF(x0, y0), QPointF(xm, y0), QPointF(x1, y0),
                QPointF(x1, ym), QPointF(x1, y1), QPointF(xm, y1),
                QPointF(x0, y1), QPointF(x0, ym),
            ):
                painter.drawRect(QRectF(point.x() - half, point.y() - half, size, size))
        finally:
            painter.restore()


_NODE_GREEN = QColor(89, 158, 130, 255)
_NODE_GREEN_FILL = QColor(232, 245, 238, 255)
_NODE_GREEN_HOVER_FILL = QColor(206, 232, 222, 255)
_NODE_ORANGE = QColor(242, 142, 28, 255)
_GUIDE_GREEN = QColor(89, 158, 130, 155)


class _StyledEndpointMixin:
    """Shared QGIS-like green/orange styling for spline edit markers."""

    def _init_style(self, layout):
        self._layout = layout
        self._normal_pen = QPen(_NODE_GREEN)
        self._normal_pen.setWidthF(1.15)
        self._normal_pen.setCosmetic(True)
        self._hover_pen = QPen(_NODE_GREEN)
        self._hover_pen.setWidthF(1.7)
        self._hover_pen.setCosmetic(True)
        self._active_pen = QPen(_NODE_ORANGE)
        self._active_pen.setWidthF(1.3)
        self._active_pen.setCosmetic(True)
        self._normal_brush = QBrush(_NODE_GREEN_FILL)
        self._hover_brush = QBrush(_NODE_GREEN_HOVER_FILL)
        self._active_brush = QBrush(_NODE_ORANGE)
        self.setState(False, False)

    def setState(self, hovered=False, active=False):
        if active:
            self.setPen(self._active_pen)
            self.setBrush(self._active_brush)
        elif hovered:
            self.setPen(self._hover_pen)
            self.setBrush(self._hover_brush)
        else:
            self.setPen(self._normal_pen)
            self.setBrush(self._normal_brush)

    def setHovered(self, hovered):
        self.setState(hovered, False)

    def paint(self, painter, option, widget=None):
        if _is_preview(self._layout):
            super().paint(painter, option, widget)


class _PreviewHandleItem(_StyledEndpointMixin, QGraphicsRectItem):
    """Constant-screen-size square Bezier control endpoint."""

    def __init__(self, layout):
        super().__init__(-4.0, -4.0, 8.0, 8.0)
        self._init_style(layout)


class _AnchorOverlayItem(_StyledEndpointMixin, QGraphicsEllipseItem):
    """Constant-screen-size circular anchor marker overlay."""

    def __init__(self, layout):
        super().__init__(-5.5, -5.5, 11.0, 11.0)
        self._init_style(layout)


class _HoverAnchorItem(_AnchorOverlayItem):
    """Backward-compatible alias for older controller code paths."""


class BezierHandleController(QObject):
    def __init__(self, view):
        super().__init__(view.viewport())
        self.view = view
        self.layout = view.currentLayout()
        self.active = None
        self._active_anchor = None
        self._handle_overlays = []
        self._overlay_keys = []
        self._anchor_overlays = []
        self._anchor_overlay_keys = []
        self._hover_target = None
        self._creation_type = None
        self._creation_drags = []
        self._creation_press_view = None
        self._creation_press_scene = None
        self._pending_creation = None
        self._creation_points = []
        self._creation_current_vector = None
        self._creation_cursor_scene = None
        self._candidate_press_view = None
        self._candidate_press_scene = None
        _CONTROLLERS_BY_VIEW[id(view)] = weakref.ref(self)
        self._guide_overlay = _GuideOverlayItem(self.layout)
        view.scene().addItem(self._guide_overlay)
        self._hover_anchor_overlay = _HoverAnchorItem(self.layout)
        self._hover_anchor_overlay.setFlag(_ignore_transformations_flag(), True)
        self._hover_anchor_overlay.setZValue(1.0e9 + 2.0)
        self._hover_anchor_overlay.setAcceptedMouseButtons(_no_button())
        self._hover_anchor_overlay.hide()
        view.scene().addItem(self._hover_anchor_overlay)
        self._creation_preview = _CreationPreviewItem(self.layout)
        view.scene().addItem(self._creation_preview)
        self._selection_bounds_overlay = _SelectionBoundsOverlayItem(self.layout)
        view.scene().addItem(self._selection_bounds_overlay)
        self._selection_bounds_item = None
        self._selection_drag_item = None
        self._selection_drag_start_scene = None
        self._selection_drag_start_rect = QRectF()
        self._selection_candidate_press_scene = None

        # Dedicated snap feedback lines for spline-anchor drags.  We cannot let
        # QgsLayoutViewToolEditNodes own these drags (it may pick a nearby node
        # from another item), so reproduce the native layout snapper feedback
        # while keeping event ownership inside this controller.
        snap_pen = QPen(QColor(255, 0, 0, 190))
        snap_pen.setWidthF(1.0)
        snap_pen.setCosmetic(True)
        snap_pen.setStyle(_dot_line())
        self._horizontal_snap_line = QGraphicsLineItem()
        self._horizontal_snap_line.setPen(snap_pen)
        self._horizontal_snap_line.setZValue(1.0e9 + 3.0)
        self._horizontal_snap_line.setAcceptedMouseButtons(_no_button())
        self._horizontal_snap_line.hide()
        view.scene().addItem(self._horizontal_snap_line)
        self._vertical_snap_line = QGraphicsLineItem()
        self._vertical_snap_line.setPen(snap_pen)
        self._vertical_snap_line.setZValue(1.0e9 + 3.0)
        self._vertical_snap_line.setAcceptedMouseButtons(_no_button())
        self._vertical_snap_line.hide()
        view.scene().addItem(self._vertical_snap_line)

        # QGraphicsView does not guarantee button-free MouseMove delivery unless
        # mouse tracking is enabled on the viewport. Preserve the original state
        # so hover highlighting can be visual-only and still work reliably.
        try:
            self._previous_mouse_tracking = view.viewport().hasMouseTracking()
            view.viewport().setMouseTracking(True)
        except RuntimeError:
            self._previous_mouse_tracking = None

        # Mouse move events can arrive much faster than the layout view can
        # repaint, especially while zooming. Coalesce them into one overlay
        # synchronization per event-loop pass instead of building a queue of
        # stale geometry updates which can visibly flicker.
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(0)
        self._sync_timer.timeout.connect(self._sync_overlay)

        view.viewport().installEventFilter(self)
        # Keyboard events (notably Backspace during node creation) are sent to
        # the QGraphicsView itself, while mouse events are sent to its viewport.
        view.installEventFilter(self)
        view.toolSet.connect(self._tool_changed)
        view.scene().selectionChanged.connect(self._selection_changed)
        if self.layout is not None:
            self.layout.itemAdded.connect(self._layout_item_added)
        self._sync_overlay()

    def detach(self, application_shutdown=False):
        # During global QGIS shutdown, Qt may already be destroying the layout
        # scene even though SIP wrappers still exist.  Calling *any* QObject or
        # QGraphicsItem method through such wrappers can hard-crash the process
        # before Python can raise/catch RuntimeError.  In that case there is
        # nothing useful to disconnect: Qt is tearing the object tree down
        # itself, so only release our pure-Python bookkeeping and return.
        if application_shutdown:
            _CONTROLLERS_BY_VIEW.pop(id(self.view), None)
            self.active = None
            self._active_anchor = None
            self._pending_creation = None
            self._hover_target = None
            return

        # QGIS may destroy the layout view/viewport (and therefore QObject
        # children such as our QTimer) before plugin unload() is called.  Keep
        # teardown idempotent and never assume wrapped Qt objects are alive.
        try:
            self._finish(cancel=True)
        except RuntimeError:
            self.active = None

        timer = getattr(self, "_sync_timer", None)
        if timer is not None:
            try:
                timer.stop()
                timer.timeout.disconnect(self._sync_overlay)
            except (TypeError, RuntimeError):
                # The C++ QTimer may already have been deleted by Qt.
                timer = None
            self._sync_timer = None
        try:
            self.view.toolSet.disconnect(self._tool_changed)
        except (TypeError, RuntimeError):
            # QGIS may already have destroyed/disconnected the layout view.
            return_value = None
            del return_value
        try:
            self.view.scene().selectionChanged.disconnect(self._selection_changed)
        except (TypeError, RuntimeError):
            # The view/scene may already be in Qt teardown.
            return_value = None
            del return_value
        if self.layout is not None:
            try:
                self.layout.itemAdded.disconnect(self._layout_item_added)
            except (TypeError, RuntimeError):
                # The layout may already have disconnected all receivers.
                return_value = None
                del return_value
        try:
            self.view.viewport().removeEventFilter(self)
            self.view.removeEventFilter(self)
            if self._previous_mouse_tracking is not None:
                self.view.viewport().setMouseTracking(self._previous_mouse_tracking)
        except RuntimeError:
            # The viewport/view can already be deleted during designer shutdown.
            return_value = None
            del return_value
        try:
            scene = self.view.scene()
            scene.removeItem(self._guide_overlay)
            scene.removeItem(self._hover_anchor_overlay)
            scene.removeItem(self._creation_preview)
            scene.removeItem(self._selection_bounds_overlay)
            scene.removeItem(self._horizontal_snap_line)
            scene.removeItem(self._vertical_snap_line)
            for handle in self._handle_overlays:
                scene.removeItem(handle)
            for anchor_overlay in self._anchor_overlays:
                scene.removeItem(anchor_overlay)
        except RuntimeError:
            # Scene deletion owns and removes the graphics items automatically.
            return_value = None
            del return_value
        self._handle_overlays.clear()
        self._overlay_keys.clear()
        self._hover_target = None
        bounds_item = getattr(self, "_selection_bounds_item", None)
        if bounds_item is not None:
            try:
                if hasattr(bounds_item, "_clear_selection_bounds_decoration"):
                    bounds_item._clear_selection_bounds_decoration()
                else:
                    bounds_item._layout_splines_draw_selection_bounds = False
                    bounds_item.update()
            except (AttributeError, RuntimeError):
                pass
            self._selection_bounds_item = None

        # Do not touch QGIS-owned mouse-handle graphics items during unload.
        # QGIS may already have destroyed their C++ instances even while stale
        # Python wrappers still exist, and calling QGraphicsItem methods on such
        # wrappers can cause a hard access violation (not a Python exception).
        _CONTROLLERS_BY_VIEW.pop(id(self.view), None)
        self._clear_creation_state()
        self._pending_creation = None



    def _current_selection_bounds_rect(self):
        """Return the visible Bezier-path selection bounds in scene coordinates."""
        item = getattr(self, "_selection_bounds_item", None)
        if item is None:
            return QRectF()
        try:
            if not item.isSelected() or item.type() not in (SPLINE_POLYGON_TYPE, SPLINE_POLYLINE_TYPE):
                return QRectF()
            local_path = item._path()
            if local_path.isEmpty():
                return QRectF()
            rect = item.mapToScene(local_path).boundingRect().normalized()
            if rect.isValid() and not rect.isNull():
                rect.adjust(-0.6, -0.6, 0.6, 0.6)
                return rect
        except (AttributeError, RuntimeError, TypeError):
            return QRectF()
        return QRectF()

    def _start_selection_drag_preview(self, scene_point):
        """Track the visible bounds during native Select/Move drags.

        QGIS moves the selected layout item after this viewport event filter
        returns. Without an active preview, the custom scene overlay keeps the
        old selected-bounds rectangle until the native drag completes. Store a
        snapshot on press and translate it by the cursor delta during the drag;
        a queued sync on release replaces it with the exact final item bounds.
        """
        if self._is_edit_nodes_tool():
            return
        item = getattr(self, "_selection_bounds_item", None)
        if item is None:
            return
        try:
            if not item.isSelected() or item.type() not in (SPLINE_POLYGON_TYPE, SPLINE_POLYLINE_TYPE):
                return
        except (AttributeError, RuntimeError, TypeError):
            return
        rect = self._current_selection_bounds_rect()
        if rect.isNull() or not rect.isValid():
            return
        self._selection_drag_item = item
        self._selection_drag_start_scene = QPointF(scene_point)
        self._selection_drag_start_rect = QRectF(rect)

    def _update_selection_drag_preview(self, scene_point):
        if self._selection_drag_item is None or self._selection_drag_start_scene is None:
            return False
        rect = QRectF(self._selection_drag_start_rect)
        if rect.isNull() or not rect.isValid():
            return False
        delta = QPointF(scene_point) - QPointF(self._selection_drag_start_scene)
        rect.translate(delta)
        try:
            self._selection_bounds_overlay.setSceneRect(rect)
        except (AttributeError, RuntimeError):
            return False
        return True

    def _clear_selection_drag_preview(self):
        self._selection_drag_item = None
        self._selection_drag_start_scene = None
        self._selection_drag_start_rect = QRectF()

    def _selection_changed(self):
        """Synchronize selection visuals immediately after selection changes.

        Using the zero-timeout coalescing timer here allows QGIS' native
        node-extents mouse-handle frame to paint for one event-loop turn before
        the spline item draws its corrected curve bounds.  Run this pass
        immediately for selection changes only; high-frequency mouse movement
        still uses the queued path.
        """
        try:
            self._sync_overlay()
        except RuntimeError:
            return

    def _queue_overlay_sync(self):
        timer = getattr(self, "_sync_timer", None)
        if timer is None:
            return
        try:
            if not timer.isActive():
                timer.start()
        except RuntimeError:
            # The view can be destroyed before plugin unload/detach runs.
            self._sync_timer = None

    def _tool_changed(self, _tool):
        if self.active is not None or self._active_anchor is not None:
            self._finish(cancel=True)
        self._clear_hover()
        self._hide_snap_lines()
        self._clear_selection_drag_preview()
        self._selection_candidate_press_scene = None
        self._clear_creation_state()
        self._pending_creation = None
        self._queue_overlay_sync()

    def _layout_item_added(self, item):
        if item is None:
            return

        # The native add-node tool creates the real layout item synchronously
        # on right-click. Attach the click-drag vectors before the GUI metadata
        # callback calls initializeBezierHandles(), so initialization can turn
        # those vectors into paired Bezier handles without replacing QGIS'
        # normal click/right-click/Backspace creation workflow.
        pending = self._pending_creation
        if pending is not None and item.type() == pending[0]:
            item._layout_splines_creation_drag_vectors = list(pending[1])

            # QGIS calls GUI metadata newItemAddedToLayout() before/around the
            # layout itemAdded signal depending on build.  Applying the data
            # only from that GUI callback is therefore racy: the temporary drag
            # vectors can arrive after initializeBezierHandles() has already
            # run.  Explicitly initialize again here, after the real item owns
            # its final snapped nodes and the vectors are attached. The method
            # is idempotent and replaces only anchors which were click-dragged.
            if hasattr(item, "initializeBezierHandles"):
                item.initializeBezierHandles()

            self._pending_creation = None
            self._clear_creation_state()

        if item.type() in (SPLINE_POLYLINE_TYPE, SPLINE_POLYGON_TYPE):
            self._queue_overlay_sync()

    def eventFilter(self, watched, event):
        event_type = event.type()

        # Capture the raw press unconditionally. On the first spline click QGIS
        # invokes createNodeRubberBand() only after this event filter returns;
        # that callback uses these coordinates to seed the new creation session.
        if event_type == _event_type("MouseButtonPress") and event.button() == _left_button():
            self._candidate_press_view = self._view_position(event)
            self._candidate_press_scene = self._scene_position(event)

        # Once createNodeRubberBand() has fired, _creation_type is authoritative.
        # Fall back to native tool introspection only for compatibility with an
        # already-active session created by an older QGIS behavior.
        creation_type = self._creation_type or self._active_creation_type()

        if event_type == _event_type("MouseButtonPress") and creation_type is not None:
            if event.button() == _left_button():
                # Do not consume the event: QGIS' native add-node tool must keep
                # ownership of node insertion, snapping, finish and
                # cancellation. We only observe the press/release drag vector.
                if self._creation_type != creation_type:
                    self._clear_creation_state()
                    self._creation_type = creation_type
                self._creation_press_view = self._view_position(event)
                self._creation_press_scene = self._scene_position(event)
                self._creation_points.append(QPointF(self._creation_press_scene))
                self._creation_drags.append(None)
                self._creation_current_vector = QPointF(0.0, 0.0)
                self._creation_cursor_scene = QPointF(self._creation_press_scene)
                self._update_creation_preview()
                self._queue_creation_preview()
                return False
            if self._is_right_button(event.button()):
                if self._creation_type == creation_type and self._creation_drags:
                    self._pending_creation = (creation_type, list(self._creation_drags))
                    # If QGIS rejects an invalid shape there will be no itemAdded
                    # signal to consume the pending data. Clear it after QGIS has
                    # finished processing this right-click event.
                    self._creation_preview.hide()
                    QTimer.singleShot(0, self._clear_stale_pending_creation)
                return False

        if event_type == _event_type("MouseButtonRelease") and creation_type is not None:
            if event.button() == _left_button() and self._creation_press_scene is not None:
                release_view = self._view_position(event)
                dx = release_view.x() - self._creation_press_view.x()
                dy = release_view.y() - self._creation_press_view.y()
                threshold = QApplication.startDragDistance()
                if math.hypot(dx, dy) >= threshold and self._creation_drags:
                    release_scene = self._scene_position(event)
                    vector = release_scene - self._creation_press_scene
                    self._creation_drags[-1] = (vector.x(), vector.y())
                self._creation_current_vector = None
                self._creation_press_view = None
                self._creation_press_scene = None
                self._creation_cursor_scene = QPointF(self._scene_position(event))
                self._update_creation_preview()
                self._queue_creation_preview()
                return False

        if event_type == _event_type("KeyPress") and creation_type is not None:
            if not event.isAutoRepeat():
                key = event.key()
                if key == self._backspace_key():
                    # Mirror QgsLayoutViewToolAddNodeItem: with one inserted node
                    # Backspace cancels, otherwise it removes the last anchor.
                    if len(self._creation_drags) > 1:
                        self._creation_drags.pop()
                        if self._creation_points:
                            self._creation_points.pop()
                        self._update_creation_preview()
                        self._queue_creation_preview()
                    else:
                        self._clear_creation_state()
                elif key == self._escape_key():
                    self._clear_creation_state()
            return False

        if event_type == _event_type("MouseMove") and creation_type is not None:
            current_scene = self._scene_position(event)
            self._creation_cursor_scene = QPointF(current_scene)
            if self._creation_press_scene is not None:
                # While the left button is held, motion means "pull this
                # anchor's Bezier handles", not "move the next temporary
                # vertex". This is the key behavioral difference from QGIS'
                # native straight-node rubber band.
                self._creation_current_vector = current_scene - self._creation_press_scene
            self._update_creation_preview()
            self._queue_creation_preview()
            return False

        if event_type == _event_type("MouseButtonPress"):
            # Select/Move drags are owned by QGIS, but our custom selection
            # bounds overlay must follow them. Cache the press point and, if a
            # spline is already selected, start a translated overlay preview.
            # If the press itself selects the spline, the first MouseMove will
            # start the preview after QGIS has updated the selection.
            if event.button() == _left_button() and self._is_select_move_tool():
                scene_point = self._scene_position(event)
                self._selection_candidate_press_scene = QPointF(scene_point)
                self._start_selection_drag_preview(scene_point)
                return False
            elif event.button() == _left_button():
                # A non-select tool may still leave a spline selected while the
                # user drags to create another item. Do not let that gesture
                # become a translated spline-bounds preview.
                self._clear_selection_drag_preview()
                self._selection_candidate_press_scene = None

            # Bezier handles and spline anchors are Edit Nodes affordances.
            # Give them first refusal before QGIS' native Edit Nodes tool sees
            # the press. Otherwise the native tool can select/move the nearest
            # node belonging to a different layout node item and silently
            # change the Items-panel selection.
            if event.button() != _left_button() or not self._is_edit_nodes_tool():
                return False

            scene_point = self._scene_position(event)
            hit = self._hit_handle(scene_point)
            if hit is not None:
                item, anchor, kind = hit
                item.beginCommand("Move Bezier Handle")
                self.active = (item, anchor, kind)
                event.accept()
                return True

            anchor_hit = self._hit_anchor(scene_point)
            if anchor_hit is not None:
                item, anchor = anchor_hit
                try:
                    item.setSelectedNode(anchor)
                except (AttributeError, RuntimeError, TypeError):
                    pass
                item.beginCommand("Move Spline Node")
                self._active_anchor = (item, anchor)
                self._clear_hover()
                event.accept()
                return True
            return False

        if event_type == _event_type("MouseMove") and self.active is not None:
            item, anchor, kind = self.active
            independent = bool(event.modifiers() & _independent_handle_modifier())
            item.setBezierHandle(
                anchor,
                kind,
                item.mapFromScene(self._scene_position(event)),
                linked=not independent,
            )
            # During an active handle drag the geometry must follow the mouse
            # immediately; there is only one update per accepted mouse event.
            self._sync_overlay()
            event.accept()
            return True

        if event_type == _event_type("MouseMove") and self._active_anchor is not None:
            item, anchor = self._active_anchor
            try:
                snapped_point = self._snap_anchor_position(item, self._scene_position(event))
                item.moveNode(anchor, snapped_point)
            except RuntimeError:
                self._active_anchor = None
                self._hide_snap_lines()
                event.accept()
                return True
            self._sync_overlay()
            event.accept()
            return True

        if event_type == _event_type("MouseButtonRelease") and (self.active is not None or self._active_anchor is not None):
            self._hide_snap_lines()
            self._finish(cancel=False)
            event.accept()
            return True

        if event_type == _event_type("MouseMove"):
            try:
                buttons = event.buttons()
            except AttributeError:
                buttons = _no_button()
            if buttons != _no_button():
                # Native item dragging happens after this filter. Keep the
                # custom selected-bounds overlay visually attached to the item
                # during Select/Move drags by translating the original bounds
                # with the mouse delta. When the press selected the spline, the
                # selection was not available at press-filter time, so lazily
                # start from the cached press point on the first drag move.
                self._clear_hover()
                if self._is_select_move_tool():
                    if self._selection_drag_item is None:
                        start_point = self._selection_candidate_press_scene or self._scene_position(event)
                        self._start_selection_drag_preview(start_point)
                    self._update_selection_drag_preview(self._scene_position(event))
                    try:
                        QTimer.singleShot(0, self._sync_native_mouse_handles_visibility)
                    except RuntimeError:
                        pass
                else:
                    # Another layout tool is using this drag (for example, Add
                    # Label/Text Box). Keep the selected spline's bounds synced
                    # to the actual item position only, never to the cursor.
                    self._clear_selection_drag_preview()
                    self._selection_candidate_press_scene = None
                self._queue_overlay_sync()
            elif self._is_edit_nodes_tool():
                # Hover assistance is deliberately visual-only. This performs
                # lightweight hit testing and changes only endpoint highlighting;
                # it never selects/moves a node or rebuilds guide geometry.
                self._update_hover(self._scene_position(event))
            else:
                self._clear_hover()
        elif event_type in (
            _event_type("MouseButtonRelease"),
            _event_type("KeyPress"),
            _event_type("KeyRelease"),
            _event_type("Wheel"),
            _event_type("Resize"),
        ):
            self._clear_hover()
            if event_type == _event_type("MouseButtonRelease"):
                self._clear_selection_drag_preview()
                self._selection_candidate_press_scene = None
            # Native QGIS tools update geometry after this filter returns.
            self._queue_overlay_sync()
        return False

    @staticmethod
    def _is_right_button(button):
        try:
            return button == Qt.MouseButton.RightButton
        except AttributeError:
            return button == Qt.RightButton

    @staticmethod
    def _backspace_key():
        try:
            return Qt.Key.Key_Backspace
        except AttributeError:
            return Qt.Key_Backspace

    @staticmethod
    def _escape_key():
        try:
            return Qt.Key.Key_Escape
        except AttributeError:
            return Qt.Key_Escape

    def _view_position(self, event):
        try:
            return event.position().toPoint()
        except AttributeError:
            return event.pos()

    def _active_creation_type(self):
        """Returns the spline type owned by the current add-node tool.

        The item metadata id is the reliable cross-version discriminator. SIP
        can wrap QgsLayoutViewToolAddNodeItem as a base Python class on some
        QGIS/Qt combinations, so a strict isinstance() test can incorrectly
        disable all creation-event observation.
        """
        try:
            tool = self.view.tool()
        except RuntimeError:
            return None
        if tool is None:
            return None

        try:
            metadata_id = tool.itemMetadataId()
            metadata = QgsGui.layoutItemGuiRegistry().itemMetadata(metadata_id)
            item_type = metadata.type() if metadata is not None else None
        except (AttributeError, RuntimeError, TypeError):
            return None

        return item_type if item_type in (SPLINE_POLYLINE_TYPE, SPLINE_POLYGON_TYPE) else None


    def _is_select_move_tool(self):
        """Return True only for QGIS' native Select/Move Item tool.

        The selected-bounds drag preview must not run while another layout
        creation tool (e.g. Add Label/Text Box, Add Map, Add Shape) is active.
        Those tools can leave a spline selected while the user drags to create
        something else; treating those drags as spline moves makes the custom
        bounds overlay follow the cursor even though the spline itself is not
        moving.  Use the view tool's class/meta-object names instead of a hard
        import so this remains tolerant of SIP wrapper differences.
        """
        try:
            tool = self.view.tool()
        except RuntimeError:
            return False
        if tool is None:
            return False

        names = [type(tool).__name__]
        try:
            names.append(str(tool.metaObject().className()))
        except (AttributeError, RuntimeError):
            pass
        joined = " ".join(names)

        # Exclude all creation/edit node tools explicitly. The select/move tool
        # is the only tool allowed to drive the translated bounds preview.
        if "EditNodes" in joined or "Add" in joined or "Create" in joined:
            return False
        return "Select" in joined or "Move" in joined

    def _is_edit_nodes_tool(self):
        try:
            tool = self.view.tool()
        except RuntimeError:
            return False
        if tool is None:
            return False
        if isinstance(tool, QgsLayoutViewToolEditNodes):
            return True
        # SIP can expose a QGIS view tool through a base-class wrapper. In that
        # case isinstance() may be false even though the underlying Qt class is
        # the native Edit Nodes tool. Accept both Python and Qt class names.
        names = [type(tool).__name__]
        try:
            names.append(str(tool.metaObject().className()))
        except (AttributeError, RuntimeError):
            return any("EditNodes" in name for name in names)
        return any("EditNodes" in name for name in names)

    def _start_creation_session(self, item_type):
        """Begins creation from QGIS' own createNodeRubberBand callback."""
        if item_type not in (SPLINE_POLYLINE_TYPE, SPLINE_POLYGON_TYPE):
            return
        self._clear_creation_state()
        self._creation_type = item_type

        # The callback runs synchronously during the first native left press.
        # eventFilter has already cached that press, so seed anchor 0 here.
        if self._candidate_press_scene is not None:
            self._creation_press_view = QPoint(self._candidate_press_view) if self._candidate_press_view is not None else None
            self._creation_press_scene = QPointF(self._candidate_press_scene)
            self._creation_points.append(QPointF(self._candidate_press_scene))
            self._creation_drags.append(None)
            self._creation_current_vector = QPointF(0.0, 0.0)
            self._creation_cursor_scene = QPointF(self._candidate_press_scene)
        self._update_creation_preview()

    def _queue_creation_preview(self):
        """Refresh the cubic preview after QGIS handles the same event.

        The viewport event filter runs before QgsLayoutViewToolAddNodeItem sees
        the mouse event. Queueing one zero-time refresh lets QGIS finish its
        snapping and node bookkeeping first, then paints from the latest
        committed spline-creation state.
        """
        try:
            QTimer.singleShot(0, self._update_creation_preview)
        except RuntimeError:
            return

    def _clear_creation_state(self):
        self._creation_type = None
        self._creation_drags = []
        self._creation_points = []
        self._creation_current_vector = None
        self._creation_cursor_scene = None
        self._creation_press_view = None
        self._creation_press_scene = None
        try:
            self._creation_preview.hide()
            empty = QPainterPath()
            self._creation_preview.setPreviewPaths(empty, empty, empty)
        except RuntimeError:
            return_value = None
            del return_value

    def _clear_stale_pending_creation(self):
        self._pending_creation = None
        self._clear_creation_state()

    def _scene_position(self, event):
        return self.view.mapToScene(self._view_position(event))

    def _update_creation_preview(self):
        """Draws a live cubic creation rubber-band overlay.

        Fixed anchors come from left-button presses. While a press is held, the
        cursor displacement is interpreted as the new anchor's paired Bezier
        vector. After release, button-free cursor motion becomes QGIS' normal
        temporary next vertex, so the preview remains useful between clicks.
        """
        if not self._creation_points:
            self._creation_preview.hide()
            return

        fixed_points = [QPointF(point) for point in self._creation_points]
        fixed_count = len(fixed_points)
        fixed_handles = []

        # Start from stable automatic handles for anchors which were simple
        # clicks. Explicit click-drag vectors override these below.
        for index, anchor in enumerate(fixed_points):
            previous = fixed_points[index - 1] if index > 0 else anchor
            following = fixed_points[index + 1] if index + 1 < fixed_count else anchor
            tx = (following.x() - previous.x()) / 6.0
            ty = (following.y() - previous.y()) / 6.0
            fixed_handles.append([-tx, -ty, tx, ty])

        for index, vector in enumerate(self._creation_drags[:fixed_count]):
            if vector is not None:
                fixed_handles[index] = [-vector[0], -vector[1], vector[0], vector[1]]

        if self._creation_press_scene is not None and self._creation_current_vector is not None:
            index = fixed_count - 1
            vector = self._creation_current_vector
            fixed_handles[index] = [-vector.x(), -vector.y(), vector.x(), vector.y()]

        # While dragging an anchor, do not treat the cursor as the next vertex:
        # the cursor is the handle endpoint. Once released, resume showing the
        # ordinary temporary next segment to the current cursor position.
        points = list(fixed_points)
        handles = [list(handle) for handle in fixed_handles]
        dragging_anchor = self._creation_press_scene is not None
        if not dragging_anchor and self._creation_cursor_scene is not None:
            cursor = QPointF(self._creation_cursor_scene)
            last = fixed_points[-1]
            if math.hypot(cursor.x() - last.x(), cursor.y() - last.y()) > 1.0e-9:
                points.append(cursor)
                handles.append([0.0, 0.0, 0.0, 0.0])

        def append_segment(path, start_index, end_index, source_points, source_handles):
            start_point = source_points[start_index]
            end_point = source_points[end_index]
            c1 = QPointF(
                start_point.x() + source_handles[start_index][2],
                start_point.y() + source_handles[start_index][3],
            )
            c2 = QPointF(
                end_point.x() + source_handles[end_index][0],
                end_point.y() + source_handles[end_index][1],
            )
            path.cubicTo(c1, c2, end_point)

        closed = self._creation_type == SPLINE_POLYGON_TYPE

        # A press-drag anchor is still the live/uncommitted anchor until mouse
        # release. Otherwise every fixed point has already been committed.
        committed_count = fixed_count - 1 if dragging_anchor else fixed_count
        committed_count = max(committed_count, 0)

        committed_path = QPainterPath()
        fill_path = QPainterPath()
        live_path = QPainterPath()

        # Solid red: geometry formed only by committed anchors. For polygons,
        # close the committed geometry exactly like the native QGIS preview so
        # the translucent fill grows as nodes are accepted.
        if committed_count >= 1:
            committed_path.moveTo(fixed_points[0])
            for index in range(committed_count - 1):
                append_segment(committed_path, index, index + 1, fixed_points, fixed_handles)

            if closed and committed_count >= 3:
                last_index = committed_count - 1
                first = fixed_points[0]
                last = fixed_points[last_index]
                c1 = QPointF(
                    last.x() + fixed_handles[last_index][2],
                    last.y() + fixed_handles[last_index][3],
                )
                c2 = QPointF(
                    first.x() + fixed_handles[0][0],
                    first.y() + fixed_handles[0][1],
                )
                committed_path.cubicTo(c1, c2, first)
                committed_path.closeSubpath()
                fill_path = QPainterPath(committed_path)

        # Dotted red: the currently uncommitted section. During click-drag this
        # is the curve into the pressed anchor (plus its temporary polygon close
        # back to anchor 0). Between clicks it is the curve to the mouse cursor.
        live_point = None
        live_handle = None
        live_from_index = None

        if dragging_anchor and fixed_count >= 1:
            live_point = fixed_points[-1]
            live_handle = fixed_handles[-1]
            if committed_count >= 1:
                live_from_index = committed_count - 1
        elif self._creation_cursor_scene is not None and fixed_count >= 1:
            cursor = QPointF(self._creation_cursor_scene)
            last = fixed_points[-1]
            if math.hypot(cursor.x() - last.x(), cursor.y() - last.y()) > 1.0e-9:
                live_point = cursor
                live_handle = [0.0, 0.0, 0.0, 0.0]
                live_from_index = fixed_count - 1

        if live_point is not None and live_from_index is not None:
            start = fixed_points[live_from_index]
            start_handle = fixed_handles[live_from_index]
            live_path.moveTo(start)
            c1 = QPointF(start.x() + start_handle[2], start.y() + start_handle[3])
            c2 = QPointF(live_point.x() + live_handle[0], live_point.y() + live_handle[1])
            live_path.cubicTo(c1, c2, live_point)

            if closed and fixed_count >= 1:
                first = fixed_points[0]
                c1 = QPointF(live_point.x() + live_handle[2], live_point.y() + live_handle[3])
                c2 = QPointF(first.x() + fixed_handles[0][0], first.y() + fixed_handles[0][1])
                live_path.cubicTo(c1, c2, first)

        visible = (not committed_path.isEmpty()) or (not live_path.isEmpty())

        self._creation_preview.setPreviewPaths(committed_path, live_path, fill_path)
        self._creation_preview.setVisible(visible)

    @staticmethod
    def _is_native_mouse_handles_item(graphics_item):
        """Returns True for QGIS' cosmetic selection/resize handles item.

        SIP can expose QgsLayoutMouseHandles through a generic graphics-item
        wrapper, so Python class-name checks are not reliable. QGIS assigns
        the mouse handles the dedicated QgsLayout::ZMouseHandles z-value
        (10000), while temporary view-tool items use different z-values. Use
        that stable scene contract first, then retain the class-name check as
        a secondary guard for builds where the wrapper name is available.
        """
        try:
            if abs(float(graphics_item.zValue()) - 10000.0) < 1.0e-9:
                return True
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False

        names = [type(graphics_item).__name__]
        try:
            names.append(str(graphics_item.metaObject().className()))
        except (AttributeError, RuntimeError):
            pass
        return any("LayoutMouseHandles" in name for name in names)

    def _set_native_mouse_handles_opacity(self, opacity):
        """Sets opacity only on mouse-handle items freshly obtained from scene.

        Never retain wrappers to QGIS-owned QGraphicsItems between event-loop
        turns. QGIS frequently deletes/recreates QgsLayoutMouseHandles as the
        active tool or selection changes. A stale SIP wrapper can survive after
        its C++ object has gone away, and even a seemingly guarded setOpacity()
        can then crash the QGIS process on Windows.
        """
        try:
            scene = self.view.scene()
            scene_items = list(scene.items())
        except (AttributeError, RuntimeError, TypeError):
            return

        for graphics_item in scene_items:
            if not self._is_native_mouse_handles_item(graphics_item):
                continue
            try:
                graphics_item.setOpacity(float(opacity))
            except (AttributeError, RuntimeError, TypeError, ValueError):
                # A handle may be recreated between scene.items() and this call.
                # Do not retain or retry the wrapper.
                continue

    def _sync_native_mouse_handles_visibility(self):
        """Keeps QGIS selection mechanics while customizing spline visuals.

        Edit Nodes hides the native frame so only anchors/Bezier handles are
        shown. For a single selected spline in Select/Move mode, the native
        frame is made transparent and the spline item draws a replacement
        frame around the visible Bezier curve extent. The native QGIS
        mouse-handle item remains alive so selection, movement and deselection
        continue to work normally.
        """
        try:
            selected = list(self.layout.selectedLayoutItems()) if self.layout is not None else []
        except (AttributeError, RuntimeError, TypeError):
            selected = []

        spline_only = (
            bool(selected)
            and all(
                item.type() in (SPLINE_POLYLINE_TYPE, SPLINE_POLYGON_TYPE)
                for item in selected
            )
        )
        edit_nodes = self._is_edit_nodes_tool()
        show_expanded_selection_bounds = (
            not edit_nodes
            and len(selected) == 1
            and selected[0].type() in (SPLINE_POLYGON_TYPE, SPLINE_POLYLINE_TYPE)
            and hasattr(selected[0], "_path")
        )

        # During normal interaction it is safe to operate on the fresh scene
        # item returned in this same synchronization pass. detach() deliberately
        # does not perform a final restore because Qt teardown may already have
        # invalidated the underlying C++ QGraphicsItem.
        self._set_native_mouse_handles_opacity(0.0 if (edit_nodes and spline_only) or show_expanded_selection_bounds else 1.0)

        previous_bounds_item = getattr(self, "_selection_bounds_item", None)
        next_bounds_item = selected[0] if show_expanded_selection_bounds else None
        if previous_bounds_item is not None and previous_bounds_item is not next_bounds_item:
            try:
                if hasattr(previous_bounds_item, "_clear_selection_bounds_decoration"):
                    previous_bounds_item._clear_selection_bounds_decoration()
                else:
                    previous_bounds_item._layout_splines_draw_selection_bounds = False
                    previous_bounds_item.update()
            except (AttributeError, RuntimeError):
                pass

        # Draw selected bounds from a transient scene overlay instead of from the
        # spline item paint routine. Item-level GUI decoration can leave stale
        # corner marks after deselection when QGIS does not repaint the old
        # decoration area. A removable overlay gives explicit cleanup: empty
        # rectangle on deselect means there is nothing left in the scene to
        # repaint or persist.
        #
        # While the native Select/Move tool is dragging the item, the custom
        # overlay is intentionally translated from the mouse delta before QGIS
        # has committed the item's final scene position. Do not recompute the
        # rectangle from the item's stored geometry during that active drag,
        # because timer/idle syncs can fire when the cursor pauses and would snap
        # the overlay back to the old/default item position until the next mouse
        # move. The drag preview remains authoritative until mouse release, and
        # the release sync then replaces it with the exact final bounds.
        active_drag_preview = (
            self._selection_drag_item is not None
            and self._selection_drag_start_scene is not None
            and next_bounds_item is self._selection_drag_item
        )
        self._selection_bounds_item = next_bounds_item
        if active_drag_preview:
            return
        overlay_rect = self._current_selection_bounds_rect() if next_bounds_item is not None else QRectF()
        try:
            self._selection_bounds_overlay.setSceneRect(overlay_rect)
        except (AttributeError, RuntimeError):
            pass

    def _selected_splines(self):
        return [
            item for item in self.view.scene().selectedItems()
            if item.type() in (SPLINE_POLYLINE_TYPE, SPLINE_POLYGON_TYPE)
            and hasattr(item, "bezierHandleScenePoints")
        ]

    def _hide_snap_lines(self):
        for line in (self._horizontal_snap_line, self._vertical_snap_line):
            try:
                line.hide()
            except RuntimeError:
                continue

    def _node_snap_candidate(self, scene_point, ignore_item):
        """Returns a nearby node from another node-based layout item.

        QgsLayoutSnapper handles guides, grids and item bounds, but item-bound
        snapping does not include the individual vertices of QgsLayoutNodesItem
        subclasses.  Add that small missing piece here so spline anchors can
        land exactly on polygon/polyline/spline nodes as well.
        """
        if self.layout is None:
            return None
        try:
            tolerance = max(1, int(self.layout.snapper().snapTolerance()))
            cursor_view = self.view.mapFromScene(scene_point)
            layout_items = list(self.layout.items())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None

        nearest = None
        nearest_pixels = float(tolerance)
        for candidate_item in layout_items:
            if candidate_item is ignore_item or not hasattr(candidate_item, "nodePosition"):
                continue
            try:
                if hasattr(candidate_item, "nodesSize"):
                    count = int(candidate_item.nodesSize())
                elif hasattr(candidate_item, "nodes"):
                    count = len(candidate_item.nodes())
                else:
                    continue
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue

            for index in range(count):
                point = QPointF()
                try:
                    if not candidate_item.nodePosition(index, point):
                        continue
                    point_view = self.view.mapFromScene(point)
                except (AttributeError, RuntimeError, TypeError):
                    continue
                distance = math.hypot(
                    point_view.x() - cursor_view.x(),
                    point_view.y() - cursor_view.y(),
                )
                if distance <= nearest_pixels:
                    nearest = QPointF(point)
                    nearest_pixels = distance
        return nearest

    def _show_node_snap_lines(self, point):
        """Shows crosshair-style tracking lines for exact node snaps."""
        try:
            rect = self.view.sceneRect()
            self._horizontal_snap_line.setLine(rect.left(), point.y(), rect.right(), point.y())
            self._vertical_snap_line.setLine(point.x(), rect.top(), point.x(), rect.bottom())
            self._horizontal_snap_line.show()
            self._vertical_snap_line.show()
        except RuntimeError:
            return

    def _snap_anchor_position(self, item, scene_point):
        """Snaps a spline anchor using the layout's configured snap settings.

        Guides retain QGIS' own highest-priority behavior, followed by item
        bounds and grid according to QgsLayoutSnapper.  If none of those snap,
        individual nodes from other node-based layout items are considered.
        The spline being edited is ignored to prevent self-snapping.
        """
        point = QPointF(scene_point)
        self._hide_snap_lines()
        if self.layout is None:
            return point

        try:
            scale = abs(float(self.view.transform().m11()))
            if scale <= 0.0:
                scale = 1.0
            snapped_point, snapped = self.layout.snapper().snapPoint(
                point,
                scale,
                self._horizontal_snap_line,
                self._vertical_snap_line,
                [item],
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            snapped_point, snapped = point, False

        if snapped:
            return QPointF(snapped_point)

        node_point = self._node_snap_candidate(point, item)
        if node_point is not None:
            self._show_node_snap_lines(node_point)
            return node_point
        return point

    def _hit_handle(self, scene_point):
        selected = self._selected_splines()
        if not selected:
            return None
        a = self.view.mapToScene(QPoint(0, 0))
        b = self.view.mapToScene(QPoint(10, 0))
        tolerance = math.hypot(b.x() - a.x(), b.y() - a.y())
        nearest, distance = None, tolerance
        for item in selected:
            for anchor, kind, point in item.bezierHandleScenePoints():
                candidate = math.hypot(
                    point.x() - scene_point.x(),
                    point.y() - scene_point.y(),
                )
                if candidate <= distance:
                    nearest, distance = (item, anchor, kind), candidate
        return nearest

    def _hit_anchor(self, scene_point):
        """Returns the nearest anchor belonging to a selected spline only.

        This deliberately does not ask the layout scene for a generic nearest
        node. QGIS' native Edit Nodes tool searches across node items and can
        therefore let an overlapping/default polygon or polyline steal a press
        intended for a spline anchor.
        """
        selected = self._selected_splines()
        if not selected:
            return None
        a = self.view.mapToScene(QPoint(0, 0))
        b = self.view.mapToScene(QPoint(10, 0))
        tolerance = math.hypot(b.x() - a.x(), b.y() - a.y())
        nearest = None
        distance = tolerance
        for item in selected:
            for anchor in range(len(item.nodes())):
                point = QPointF()
                try:
                    valid = item.nodePosition(anchor, point)
                except RuntimeError:
                    valid = False
                if not valid:
                    continue
                candidate = math.hypot(point.x() - scene_point.x(), point.y() - scene_point.y())
                if candidate <= distance:
                    nearest = (item, anchor)
                    distance = candidate
        return nearest

    def _hover_hit(self, scene_point):
        selected = self._selected_splines()
        if not selected:
            return None
        a = self.view.mapToScene(QPoint(0, 0))
        b = self.view.mapToScene(QPoint(10, 0))
        tolerance = math.hypot(b.x() - a.x(), b.y() - a.y())

        # Handles win when an anchor and a handle overlap on screen.
        handle_hit = self._hit_handle(scene_point)
        if handle_hit is not None:
            item, anchor, kind = handle_hit
            return ("handle", item, anchor, kind)

        nearest = None
        distance = tolerance
        for item in selected:
            for anchor in range(len(item.nodes())):
                point = QPointF()
                if not item.nodePosition(anchor, point):
                    continue
                candidate = math.hypot(point.x() - scene_point.x(), point.y() - scene_point.y())
                if candidate <= distance:
                    nearest = ("anchor", item, anchor)
                    distance = candidate
        return nearest

    def _update_hover(self, scene_point):
        target = self._hover_hit(scene_point)
        if target == self._hover_target:
            return
        self._hover_target = target
        self._refresh_hover_visual()

    def _clear_hover(self):
        if self._hover_target is None:
            return
        self._hover_target = None
        self._refresh_hover_visual()

    def _refresh_hover_visual(self):
        target = self._hover_target
        hovered_handle_key = None
        hovered_anchor_key = None
        if target is not None and target[0] == "handle":
            hovered_handle_key = (target[1], target[2], target[3])
        elif target is not None and target[0] == "anchor":
            hovered_anchor_key = (target[1], target[2])

        active_handle_key = None
        if self.active is not None:
            active_handle_key = (self.active[0], self.active[1], self.active[2])
        active_anchor_key = None
        if self._active_anchor is not None:
            active_anchor_key = (self._active_anchor[0], self._active_anchor[1])

        for index, handle in enumerate(self._handle_overlays):
            key = self._overlay_keys[index] if index < len(self._overlay_keys) else None
            handle.setState(key == hovered_handle_key, key == active_handle_key)

        for index, anchor_overlay in enumerate(self._anchor_overlays):
            key = self._anchor_overlay_keys[index] if index < len(self._anchor_overlay_keys) else None
            anchor_overlay.setState(key == hovered_anchor_key, key == active_anchor_key)

        # The previous single hover-ring overlay is no longer used now that all
        # selected anchors have native-looking circular overlays. Hide it if it
        # exists so only the current marker set is visible.
        try:
            self._hover_anchor_overlay.hide()
        except RuntimeError:
            return

    def _new_handle_overlay(self):
        scene = self.view.scene()
        handle = _PreviewHandleItem(self.view.currentLayout())
        handle.setFlag(_ignore_transformations_flag(), True)
        handle.setZValue(1.0e9 + 2.0)
        handle.setAcceptedMouseButtons(_no_button())
        handle.hide()
        scene.addItem(handle)
        self._handle_overlays.append(handle)

    def _new_anchor_overlay(self):
        scene = self.view.scene()
        anchor_overlay = _AnchorOverlayItem(self.view.currentLayout())
        anchor_overlay.setFlag(_ignore_transformations_flag(), True)
        anchor_overlay.setZValue(1.0e9 + 2.1)
        anchor_overlay.setAcceptedMouseButtons(_no_button())
        anchor_overlay.hide()
        scene.addItem(anchor_overlay)
        self._anchor_overlays.append(anchor_overlay)

    def _sync_overlay(self):
        self._sync_native_mouse_handles_visibility()

        # Match QGIS' native node-editing model: Bezier handles, guide lines and
        # anchor hover decoration are visible only while Edit Nodes is active.
        # The spline itself remains selectable/movable with Select/Move Item.
        if not self._is_edit_nodes_tool():
            self._guide_overlay.setLines([])
            self._overlay_keys = []
            self._hover_target = None
            try:
                self._hover_anchor_overlay.hide()
            except RuntimeError:
                pass
            for handle in self._handle_overlays:
                try:
                    handle.setHovered(False)
                    handle.hide()
                except RuntimeError:
                    continue
            for anchor_overlay in self._anchor_overlays:
                try:
                    anchor_overlay.setState(False, False)
                    anchor_overlay.hide()
                except RuntimeError:
                    continue
            return

        points = []
        keys = []
        anchor_points = []
        anchor_keys = []
        guide_lines = []
        try:
            for item in self._selected_splines():
                # Snapshot both native anchors and Bezier handles in one pass.
                # nodePosition() is QGIS' public scene-coordinate API for layout
                # node items, so it remains authoritative while Edit Nodes is
                # active and avoids duplicating native coordinate conversion.
                nodes = list(item.nodes())
                for anchor in range(len(nodes)):
                    anchor_scene = QPointF()
                    if item.nodePosition(anchor, anchor_scene):
                        anchor_points.append(QPointF(anchor_scene))
                        anchor_keys.append((item, anchor))
                for anchor, kind, handle_scene in item.bezierHandleScenePoints():
                    anchor_scene = QPointF()
                    if item.nodePosition(anchor, anchor_scene):
                        points.append(handle_scene)
                        keys.append((item, anchor, kind))
                        guide_lines.append(QLineF(anchor_scene, handle_scene))
        except RuntimeError:
            # A selected item may disappear while the designer is closing.
            points = []
            keys = []
            anchor_points = []
            anchor_keys = []
            guide_lines = []

        while len(self._handle_overlays) < len(points):
            self._new_handle_overlay()
        while len(self._anchor_overlays) < len(anchor_points):
            self._new_anchor_overlay()

        # One atomic guide snapshot eliminates transient combinations of old
        # and new line endpoints during hover/zoom repaints.
        self._guide_overlay.setLines(guide_lines)

        self._overlay_keys = keys
        self._anchor_overlay_keys = anchor_keys
        for index, handle in enumerate(self._handle_overlays):
            if index < len(points):
                handle.setPos(points[index])
                handle.show()
            else:
                handle.setState(False, False)
                handle.hide()
        for index, anchor_overlay in enumerate(self._anchor_overlays):
            if index < len(anchor_points):
                anchor_overlay.setPos(anchor_points[index])
                anchor_overlay.show()
            else:
                anchor_overlay.setState(False, False)
                anchor_overlay.hide()
        self._refresh_hover_visual()

    def _finish(self, cancel=False):
        if self.active is None and self._active_anchor is None:
            return
        item = self.active[0] if self.active is not None else self._active_anchor[0]
        self._hide_snap_lines()
        self.active = None
        self._active_anchor = None
        try:
            if cancel:
                item.cancelCommand()
            else:
                item.endCommand()
        except RuntimeError:
            # The item can be deleted while an edit command is active.
            return_value = None
            del return_value
        try:
            self._sync_overlay()
        except RuntimeError:
            # The layout view can already be gone during QGIS teardown.
            return
