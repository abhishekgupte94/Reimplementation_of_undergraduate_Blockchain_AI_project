# SSD pen simulation

The demonstration uses TensorFlow's MobileNet V2 Single Shot Detector trained on Open Images V4. The application accepts class ID 569, **Pen** (`/m/0k1tl`), and discards the other model outputs. An alert records the pen box, room, capture generation, observation time, score and model version. The same frame can separately produce face observations. [TensorFlow label map](https://github.com/tensorflow/models/blob/master/research/object_detection/data/oid_v4_label_map.pbtxt#L2842-L2845).

## Install and verify

```sh
flask --app surveillance download-weapons --with-sample
flask --app surveillance weapon-smoke
flask --app surveillance weapon-smoke --image instance/private/models/weapons/pen-test.jpg --expect Pen
```

The installer verifies SHA-256 digests for the TensorFlow archive, OpenCV 4.12 graph conversion scripts and generated inference files. It downloads files only when needed. The frozen graph and generated OpenCV text graph are installed under `instance/private/models/weapons/`. OpenCV runs them on CPU with 300×300 RGB inputs and applies non-maximum suppression in the graph. Restart the web process to load SSD changes. [TensorFlow detection model zoo](https://github.com/tensorflow/models/blob/master/research/object_detection/g3doc/tf1_detection_zoo.md), [OpenCV TensorFlow import](https://github.com/opencv/opencv/wiki/TensorFlow-Object-Detection-API).

The positive example is Open Images validation image `0a8fc50cd3e3df1a`, “Ouderwetse brievenpost” by Daphne Depasse, licensed CC BY 2.0. The smoke command checks inference independently of the webcam and database. The example scored 0.756 with OpenCV 4.12 and the blank frame produced zero detections. Two other labeled pen photos tested during setup did not produce a pen detection. Scores are model outputs and performance varies with the pen, angle and lighting. [Original photo](https://www.flickr.com/photos/daphnedepasse/15976898225), [CC BY 2.0](https://creativecommons.org/licenses/by/2.0/), [Open Images V4 annotations](https://storage.googleapis.com/openimages/web/download_v4.html).

## Camera and database coordination

Face recognition commits its observations before the camera route offers a sample to SSD. The local web process has one SSD worker with at most one 300×300 sample in flight. Incoming frames continue through recognition while SSD loads or processes a sample. Recent SSD overlays are scoped to the same capture, generation, room and image dimensions.

The SSD worker uses its own SQLAlchemy transaction. It skips the alert write if the camera lock or database writer is busy. Inside the lock it checks the capture lease and deduplicates against recent pen alerts. SSD writes its own alert and audit outbox entry in the same transaction. Capture expiry, face observations and presence intervals are managed by the face pipeline. The shared lock keeps an alert commit atomic with room switching or capture stop.

## Administrator alerts

Visible, signed-in administrator pages use `fetch` to check for the latest unacknowledged pen alert every three seconds. The banner offers acknowledgement directly and links to **SSD alerts**, which fetches the latest 100 history entries. Earlier model observations remain in that history. Polling pauses on hidden pages, resumes when visible and stops if the server rejects the session. Participants can see the pen overlay on their camera view.

- `GET /api/admin/weapon-alerts?pending=1` returns the latest unacknowledged pen alert for the current model.
- `GET /api/admin/weapon-alerts` also returns recent history.
- `POST /api/admin/weapon-alerts/<id>/acknowledge` requires an administrator session and a CSRF token. A repeated request returns success without a duplicate audit entry.

## Configuration

| Setting | Default |
| --- | --- |
| `WEAPON_DETECTION_ENABLED` | True for the live app, false for isolated tests |
| `WEAPON_MODEL_DIR` | `PRIVATE_DIR/models/weapons` |
| `WEAPON_CONFIDENCE` | `0.55`, accepted range `[0.3, 1)` |
| `WEAPON_COOLDOWN_SECONDS` | `10` |
| `WEAPON_RESULT_MAX_AGE_SECONDS` | `3` |
