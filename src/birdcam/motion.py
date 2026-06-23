"""Cheap frame-difference motion detection, shared by both engines. The caller compares the
returned changed-pixel count against MOTION_THRESHOLD and passes the returned blurred frame
back in as prev_frame next time."""
import cv2


def detect_motion(prev_frame, gray):
    blurred = cv2.GaussianBlur(gray, (21, 21), 0)
    if prev_frame is None:
        return 0, blurred
    diff = cv2.absdiff(prev_frame, blurred)
    thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)[1]
    return cv2.countNonZero(thresh), blurred
