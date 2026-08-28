"""QGIS Layout Splines plugin entry point."""


def classFactory(iface):
    from .plugin import LayoutSplinesPlugin

    return LayoutSplinesPlugin(iface)
