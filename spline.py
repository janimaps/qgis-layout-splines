"""Cubic Bezier helpers using anchors plus attached handle offsets."""

from qgis.PyQt.QtCore import QPointF
from qgis.PyQt.QtGui import QPainterPath, QPolygonF


SAMPLES_PER_SEGMENT = 28


def default_handles(points, closed=False):
    """Return [in_x, in_y, out_x, out_y] offsets for each anchor."""
    pts = [QPointF(point) for point in points]
    count = len(pts)
    handles = []
    for index, anchor in enumerate(pts):
        previous = pts[(index - 1) % count] if closed or index > 0 else anchor
        following = pts[(index + 1) % count] if closed or index + 1 < count else anchor
        tx = (following.x() - previous.x()) / 6.0
        ty = (following.y() - previous.y()) / 6.0
        handles.append([-tx, -ty, tx, ty])
    return handles


def handles_from_explicit_nodes(points, closed=False):
    """Migrate the unstable v1.1 anchor/control/control node representation."""
    pts = [QPointF(point) for point in points]
    valid = len(pts) >= 3 and (len(pts) % 3 == 0 if closed else (len(pts) - 1) % 3 == 0)
    if not valid:
        return pts, default_handles(pts, closed)
    anchors = pts[0::3]
    handles = []
    for index, anchor in enumerate(anchors):
        anchor_node = index * 3
        if closed:
            incoming = pts[(anchor_node - 1) % len(pts)]
            outgoing = pts[anchor_node + 1]
        else:
            incoming = pts[anchor_node - 1] if index > 0 else anchor
            outgoing = pts[anchor_node + 1] if index + 1 < len(anchors) else anchor
        handles.append([
            incoming.x() - anchor.x(), incoming.y() - anchor.y(),
            outgoing.x() - anchor.x(), outgoing.y() - anchor.y(),
        ])
    return anchors, handles


def bezier_path(points, handles, closed=False):
    pts = [QPointF(point) for point in points]
    path = QPainterPath()
    if not pts:
        return path
    if len(handles) != len(pts):
        handles = default_handles(pts, closed)
    path.moveTo(pts[0])
    segment_count = len(pts) if closed else max(len(pts) - 1, 0)
    for index in range(segment_count):
        next_index = (index + 1) % len(pts)
        start, end = pts[index], pts[next_index]
        c1 = QPointF(start.x() + handles[index][2], start.y() + handles[index][3])
        c2 = QPointF(end.x() + handles[next_index][0], end.y() + handles[next_index][1])
        path.cubicTo(c1, c2, end)
    if closed:
        path.closeSubpath()
    return path


def sampled_bezier(points, handles, closed=False, samples=SAMPLES_PER_SEGMENT):
    pts = list(points)
    if len(pts) < 2:
        return QPolygonF(pts)
    path = bezier_path(pts, handles, closed)
    segments = len(pts) if closed else len(pts) - 1
    total = max(segments * samples, 1)
    result = QPolygonF([path.pointAtPercent(step / total) for step in range(total + 1)])
    if closed and result and result[-1] != result[0]:
        result.append(result[0])
    return result


def handle_points(points, handles, closed=False):
    pts = list(points)
    if len(handles) != len(pts):
        return
    for index, anchor in enumerate(pts):
        if closed or index > 0:
            yield index, "in", QPointF(anchor.x() + handles[index][0], anchor.y() + handles[index][1])
        if closed or index + 1 < len(pts):
            yield index, "out", QPointF(anchor.x() + handles[index][2], anchor.y() + handles[index][3])
