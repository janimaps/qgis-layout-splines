"""Plugin registration for the two layout spline item types."""

from qgis.core import QgsApplication, QgsLayoutItemRegistry, QgsMessageLog, Qgis
from qgis.PyQt.QtCore import QCoreApplication
from qgis.gui import QgsGui

from .items import (
    SPLINE_POLYGON_TYPE,
    SPLINE_POLYLINE_TYPE,
    SplinePolygon,
    SplinePolyline,
)
from .metadata import SplineGuiMetadata, SplineItemMetadata
from .handle_controller import BezierHandleController
from .recovery import recover_layout


class LayoutSplinesPlugin:
    """Registers layout item metadata globally for all Layout Designers."""

    def __init__(self, iface):
        self.iface = iface
        self._metadata = []
        self._controllers = {}
        self._application_shutting_down = False

    def initGui(self):
        core_registry = QgsApplication.layoutItemRegistry()
        gui_registry = QgsGui.layoutItemGuiRegistry()

        definitions = (
            (
                SPLINE_POLYGON_TYPE,
                self.tr("Spline Polygon"),
                self.tr("Spline Polygons"),
                SplinePolygon,
                QgsLayoutItemRegistry.LayoutPolygon,
                "icons/spline_polygon.png",
            ),
            (
                SPLINE_POLYLINE_TYPE,
                self.tr("Spline Polyline"),
                self.tr("Spline Polylines"),
                SplinePolyline,
                QgsLayoutItemRegistry.LayoutPolyline,
                "icons/spline_polyline.png",
            ),
        )

        for type_id, name, plural, item_class, base_type, icon_path in definitions:
            # Registries intentionally own metadata for the QGIS process lifetime and
            # do not offer removal APIs. Avoid duplicates when Plugin Reloader is used.
            if core_registry.itemMetadata(type_id) is None:
                core_meta = SplineItemMetadata(
                    type_id, name, plural, item_class
                )
                if core_registry.addLayoutItemType(core_meta):
                    self._metadata.append(core_meta)

            if gui_registry.metadataIdForItemType(type_id) < 0:
                gui_meta = SplineGuiMetadata(
                    type_id, name, base_type, icon_path
                )
                if gui_registry.addLayoutItemGuiMetadata(gui_meta):
                    self._metadata.append(gui_meta)

        self._log("Spline layout items registered")
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._about_to_quit)
        self.iface.layoutDesignerOpened.connect(self._designer_opened)
        self.iface.layoutDesignerWillBeClosed.connect(self._designer_closing)
        for designer in self.iface.openLayoutDesigners():
            self._designer_opened(designer)

    def unload(self):
        # QgsPluginRegistry also calls unload() while QGIS itself is shutting
        # down.  At that stage SIP wrappers may still be non-None even though
        # their C++ objects are already being destroyed.  QObject.disconnect()
        # can then cause a native access violation, so global-shutdown unload is
        # intentionally Python-only. Qt owns and tears down the object tree.
        shutting_down = self._is_application_shutting_down()
        self._metadata.clear()
        if shutting_down:
            for controller in list(self._controllers.values()):
                controller.detach(application_shutdown=True)
            self._controllers.clear()
            return

        try:
            self.iface.layoutDesignerOpened.disconnect(self._designer_opened)
        except (TypeError, RuntimeError):
            self._log("Layout designer opened signal was already disconnected")
        try:
            self.iface.layoutDesignerWillBeClosed.disconnect(self._designer_closing)
        except (TypeError, RuntimeError):
            self._log("Layout designer closing signal was already disconnected")

        app = QCoreApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.disconnect(self._about_to_quit)
            except (TypeError, RuntimeError):
                self._log("Application shutdown signal was already disconnected")

        for controller in list(self._controllers.values()):
            controller.detach()
        self._controllers.clear()

    def _about_to_quit(self):
        self._application_shutting_down = True

    def _is_application_shutting_down(self):
        if self._application_shutting_down:
            return True
        try:
            return bool(QCoreApplication.closingDown())
        except (AttributeError, RuntimeError):
            return False

    def _designer_opened(self, designer):
        view = designer.view()
        if view is None:
            return
        layout = view.currentLayout()
        if layout is not None:
            restored = recover_layout(
                layout,
                {
                    SPLINE_POLYGON_TYPE: SplinePolygon,
                    SPLINE_POLYLINE_TYPE: SplinePolyline,
                },
            )
            if restored:
                self._log(f"Recovered {len(restored)} preserved spline layout item(s)")
        if id(designer) not in self._controllers:
            self._controllers[id(designer)] = BezierHandleController(view)

    def _designer_closing(self, designer):
        controller = self._controllers.pop(id(designer), None)
        if controller is not None: controller.detach()

    @staticmethod
    def tr(text):
        from qgis.PyQt.QtCore import QCoreApplication

        return QCoreApplication.translate("LayoutSplines", text)

    @staticmethod
    def _log(message):
        try:
            level = Qgis.MessageLevel.Info
        except AttributeError:  # QGIS 3 compatibility alias
            level = Qgis.Info
        QgsMessageLog.logMessage(message, "Layout Splines", level)
