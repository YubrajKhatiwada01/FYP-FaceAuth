"""
aws_iot_door.py — AWS IoT Core door-unlock trigger for FaceAuth.

Usage (standalone function)
---------------------------
    from aws_iot_door import trigger_door_unlock

    trigger_door_unlock(user_id=42)

Usage (integrated into the recognition while-loop)
---------------------------------------------------
See the bottom of this file for a self-contained demo loop, or copy the
pattern into your own script.  The key idea is:

    1.  Keep a consecutive-match counter per session.
    2.  On the FIRST frame that passes the threshold, fire the MQTT message.
    3.  Record the timestamp and refuse to fire again for COOLDOWN_SECONDS.
"""

import json
import logging
import time

from aws_config import AWS_REGION, get_iot_data          # re-uses the thread-local session

logger = logging.getLogger(__name__)

# ── Tuneable constants ────────────────────────────────────────────────────────

DOOR_TOPIC         = "server_room/door_command"   # must match ESP32 subscription
COOLDOWN_SECONDS   = 6       # minimum gap between publishes (matches the 5-second physical door open hold)
FRAMES_REQUIRED    = 1       # immediate unlock trigger on face verification


# ── Module-level cooldown state (one state per Python process) ────────────────

_last_trigger_time: float = 0.0     # epoch seconds of the last successful publish



# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def trigger_door_unlock(
    user_id,
    full_name:   str       = "Unknown",
    region_name: str | None = None,
) -> bool:
    """
    Publish a JSON door-command MQTT message to AWS IoT Core.

    Publishes to DOOR_TOPIC with payload::

        {"status": "GRANTED", "name": "<full_name>"}

    The ESP32 Arduino callback parses this JSON and:
      * Displays "ACCESS GRANTED / <name>" on the 16x2 I2C LCD
      * Rotates the servo to 90° for 5 seconds, then returns to 0°

    Respects a module-level cooldown so that repeated calls within
    ``COOLDOWN_SECONDS`` seconds are silently ignored (returns False).

    Parameters
    ----------
    user_id : int | str
        The authenticated user's database ID (used for logging only).
    full_name : str
        The user's display name shown on the LCD (e.g. "John Doe").
        Defaults to "Unknown" if not supplied.
    region_name : str
        AWS region of the IoT Core broker.  Must match ``aws_config.py``.

    Returns
    -------
    bool
        True  — MQTT message was published successfully.
        False — Either still within cooldown window, or publish failed.
    """
    global _last_trigger_time

    # ── Cooldown check ────────────────────────────────────────────────────────
    now = time.time()
    elapsed = now - _last_trigger_time
    if elapsed < COOLDOWN_SECONDS:
        remaining = round(COOLDOWN_SECONDS - elapsed, 1)
        logger.debug(
            "Door trigger skipped for user_id=%s — cooldown active (%ss remaining).",
            user_id, remaining,
        )
        return False

    # ── Build JSON payload ────────────────────────────────────────────────────
    # ESP32 mqttCallback() expects: {"status": "GRANTED", "name": "<name>"}
    payload = json.dumps({
        "status": "GRANTED",
        "name":   full_name or "Unknown",
    })

    target_region = region_name or AWS_REGION

    # ── Publish to IoT Core ───────────────────────────────────────────────────
    try:
        client = get_iot_data(region_name=target_region)
        response = client.publish(
            topic   = DOOR_TOPIC,
            qos     = 1,          # at-least-once delivery
            payload = payload,
        )
        http_status = response.get("ResponseMetadata", {}).get("HTTPStatusCode", "?")
        logger.info(
            "Door unlock published for user_id=%s name='%s' -> topic='%s' (HTTP %s).",
            user_id, full_name, DOOR_TOPIC, http_status,
        )
        _last_trigger_time = now   # only update timestamp on success
        return True

    except Exception as exc:      # noqa: BLE001
        # Catches: NoCredentialsError, EndpointResolutionError, network errors, etc.
        logger.error(
            "Failed to publish door unlock for user_id=%s: %s",
            user_id, exc,
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown-aware camera-loop helper
# ─────────────────────────────────────────────────────────────────────────────

class DoorTriggerController:
    """
    Stateful helper that wraps trigger_door_unlock() with:

    * A **consecutive-match counter** — the door only fires after
      FRAMES_REQUIRED frames in a row report a match for the *same* user.
    * The **cooldown** is already enforced inside trigger_door_unlock().

    Typical usage inside a ``while True`` camera loop::

        controller = DoorTriggerController()

        while True:
            result = recognize_face(frame_bytes, user_id=user.id, ...)
            if result["match"]:
                controller.on_match(user.id)
            else:
                controller.reset()
    """

    def __init__(
        self,
        frames_required: int = FRAMES_REQUIRED,
        region_name:     str | None = None,
    ) -> None:
        self.frames_required = frames_required
        self.region_name     = region_name or AWS_REGION
        self._count:    int        = 0
        self._last_uid             = None

    def on_match(self, user_id, full_name: str = "Unknown") -> bool:
        """
        Call this every frame that returns match=True.

        Parameters
        ----------
        user_id : int | str
            The authenticated user's database ID.
        full_name : str
            The user's display name — shown on the ESP32 LCD.
            Pass the ``full_name`` field from your recognition result.

        Returns True if the door-unlock MQTT message was actually sent
        this frame (i.e. threshold was just reached AND cooldown has expired).
        """
        # Reset counter when the matched user changes mid-session
        if self._last_uid != user_id:
            self._count    = 0
            self._last_uid = user_id

        self._count += 1
        logger.debug(
            "Consecutive match count for user_id=%s: %d/%d",
            user_id, self._count, self.frames_required,
        )

        if self._count >= self.frames_required:
            fired = trigger_door_unlock(
                user_id,
                full_name   = full_name,
                region_name = self.region_name,
            )
            if fired:
                # Reset so the next unlock requires another FRAMES_REQUIRED
                # consecutive matches (after the cooldown window passes).
                self._count = 0
            return fired
        return False

    def reset(self) -> None:
        """
        Call this every frame where match=False (face lost / mismatched).
        Resets the consecutive-match counter so a single bad frame doesn't
        carry over into the next recognition run.
        """
        if self._count > 0:
            logger.debug(
                "Match streak broken for user_id=%s after %d frame(s).",
                self._last_uid, self._count,
            )
        self._count    = 0
        self._last_uid = None


# ─────────────────────────────────────────────────────────────────────────────
# Demo / reference while-True loop
# ─────────────────────────────────────────────────────────────────────────────

def _demo_camera_loop(user_id: int, camera_index: int = 0) -> None:
    """
    Self-contained demonstration of the full integration.

    Copy this pattern into your own script.  Replace the placeholder
    photo_path with your real S3 key or local filename.

    Run:
        python aws_iot_door.py
    """
    import cv2                                                # pip install opencv-python
    from face_recognition_service import recognize_face      # your existing service

    cap        = cv2.VideoCapture(camera_index)
    controller = DoorTriggerController(frames_required=FRAMES_REQUIRED)

    print(f"[demo] Starting camera loop.  user_id={user_id}  Press Q to quit.")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                logger.warning("Camera read failed — retrying in 0.1 s.")
                time.sleep(0.1)
                continue

            # ── Encode frame to JPEG bytes ────────────────────────────────────
            ok, buffer = cv2.imencode('.jpg', frame)
            if not ok:
                continue
            frame_bytes = buffer.tobytes()

            # ── Run facial recognition ────────────────────────────────────────
            # Replace photo_path with your real enrolled S3 key.
            result = recognize_face(
                image_bytes = frame_bytes,
                user_id     = user_id,
                photo_path  = "photos/your_enrolled_photo.jpg",
            )

            # ── Feed result into the controller ───────────────────────────────
            if result.get("match"):
                # Pass full_name so the ESP32 LCD shows the user's name
                name  = result.get("full_name", "Unknown")
                fired = controller.on_match(user_id, full_name=name)
                conf  = result.get("confidence", 0.0)
                label = f"MATCH  {conf:.1f}%  [{name}]"
                color = (0, 255, 0)   # green

                if fired:
                    print(f"[DOOR] JSON GRANTED sent to IoT Core — user_id={user_id}, name='{name}'")
                    label += "  -> DOOR OPEN"
            else:
                controller.reset()
                label = result.get("error") or "No match"
                color = (0, 0, 255)   # red

            # ── Draw HUD on frame ─────────────────────────────────────────────
            cv2.putText(
                frame, label, (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2,
            )
            cv2.imshow("FaceAuth — Door Control", frame)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

    finally:
        cap.release()
        cv2.destroyAllWindows()
        print("[demo] Camera loop stopped.")


if __name__ == "__main__":
    logging.basicConfig(
        level  = logging.INFO,
        format = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    )
    # Replace 1 with your actual user_id from the database
    _demo_camera_loop(user_id=1)
