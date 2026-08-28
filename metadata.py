"""Core and GUI registry metadata usable from Python bindings."""

import os

from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsApplication, QgsLayoutItemAbstractMetadata
from qgis.gui import QgsGui, QgsLayoutItemAbstractGuiMetadata


class SplineItemMetadata(QgsLayoutItemAbstractMetadata):
    def __init__(self, type_id, name, plural_name, item_class):
        super().__init__(type_id, name, plural_name)
        self._item_class = item_class

    def createItem(self, layout):
        return self._item_class(layout)


class SplineGuiMetadata(QgsLayoutItemAbstractGuiMetadata):
    """GUI metadata delegating native widgets/rubber bands to base items."""

    def __init__(self, type_id, name, base_type, relative_icon):
        # QGIS registers its Polygon and Polyline actions in the "nodes"
        # group. Reusing that id appends these actions to the same button.
        super().__init__(type_id, name, "nodes", True)
        self._base_type = base_type
        self._icon = QIcon(os.path.join(os.path.dirname(__file__), relative_icon))

    def creationIcon(self):
        return self._icon

    def createItem(self, layout):
        return QgsApplication.layoutItemRegistry().createItem(self.type(), layout)

    def createItemWidget(self, item):
        base = self._base_metadata()
        return base.createItemWidget(item) if base is not None else None

    def createNodeRubberBand(self, view):
        """Creates QGIS' native node carrier, but keeps it fully invisible.

        QgsLayoutViewToolAddNodeItem uses the returned node rubber band as its
        persistent geometry accumulator while successive anchors are inserted.
        Returning ``None`` is legal at the API level, but in QGIS 3.x it means
        the add-node tool has no object in which to retain the existing anchor
        chain, so every new click can replace the previous point.  We therefore
        delegate creation to the corresponding native Polygon/Polyline GUI
        metadata and make that graphics item transparent.  It remains strictly
        an internal carrier; the controller's cubic spline overlay is the only
        creation preview shown to the user.
        """
        creation_notified = False
        try:
            from .handle_controller import notify_creation_started
            notify_creation_started(view, self.type())
            creation_notified = True
        except (ImportError, RuntimeError, TypeError):
            # Creation can race with a designer being destroyed. The native
            # carrier below is still preferable when the base metadata exists.
            creation_notified = False

        base = self._base_metadata()
        if base is None:
            return None

        # Keep this assignment explicit so teardown-time notification failure
        # does not change the carrier behavior.
        _ = creation_notified

        try:
            rubber_band = base.createNodeRubberBand(view)
        except (AttributeError, RuntimeError, TypeError):
            return None

        if rubber_band is None:
            return None

        # Opacity keeps the native object alive, in-scene and fully usable by
        # QgsLayoutViewToolAddNodeItem without allowing its straight red path to
        # compete with the plugin's curved spline preview. Unlike setVisible(False),
        # this does not change the item's participation in scene bookkeeping.
        try:
            rubber_band.setOpacity(0.0)
        except (AttributeError, RuntimeError, TypeError):
            # Some bindings may expose a non-QGraphicsItem wrapper. In that
            # unlikely case return it unchanged rather than breaking creation.
            return rubber_band

        return rubber_band

    def newItemAddedToLayout(self, item):
        # QGIS has now finished collecting the user-clicked anchors. Expand
        # them into real cubic control nodes for its native Edit Nodes tool.
        if hasattr(item, "initializeBezierHandles"):
            item.initializeBezierHandles()

    def _base_metadata(self):
        registry = QgsGui.layoutItemGuiRegistry()
        metadata_id = registry.metadataIdForItemType(self._base_type)
        return registry.itemMetadata(metadata_id) if metadata_id >= 0 else None
