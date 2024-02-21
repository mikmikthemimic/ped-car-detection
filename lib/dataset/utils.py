def validate_boxes(boxes, id, width=0, height=0):
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    assert (x1 >= 0).all(), f"x1 < 0 on {id} | {x1}"
    assert (y1 >= 0).all(), f"y1 < 0 on {id} | {y1}"
    assert (x2 >= x1).all(), f"x2 < x1 on {id} | {x2}, {x1}\n{boxes}"
    assert (y2 >= y1).all(), f"y2 < y1 on {id} | {y2}, {y1}\n{boxes}"
    assert (x2 < width).all(), f"x2 > width on {id} | {width}, {x2}\n{boxes}"
    assert (y2 < height).all(), f"y2 > height on {id} | {height}, {y2}\n{boxes}"