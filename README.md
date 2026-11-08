# Block A.I

A local rebuild of a 2019 surveillance demonstration. Participants enroll photos and use one webcam to simulate visits to two rooms. Administrators can compare observed room time, ask the activity chatbot about visits and sightings, and review alerts from an SSD pen detection simulation.

Flask serves the app, MySQL stores operational records, and a local Anvil development chain anchors audit hashes. Face and pen inference run locally through OpenCV. The demo can run offline after setup.

## Set up a fresh checkout

The integrated launcher has been tested on macOS with **Python 3.13**, **Node.js and npm**, and **MySQL 8.4**. Install MySQL with Homebrew, then run the remaining commands from the checkout root.

```sh
brew install mysql@8.4
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -c requirements.lock -e '.[dev]'
npm ci --ignore-scripts
python scripts/setup_mysql.py
flask --app surveillance download-model
flask --app surveillance download-weapons --with-sample
flask --app surveillance create-admin
```

`setup_mysql.py` creates the local database, runs the migrations and generates a private `.env`. The administrator command prompts for a password. To use a separately managed MySQL server, configure `.env` from [`.env.example`](.env.example), create the database, and run `flask --app surveillance db upgrade`. See [MySQL and ledger](docs/mysql-ledger.md) for the individual Flask and worker commands, database access, SQLite migration and recovery.

The model download commands verify checksums and store weights under `instance/private/models/`. See [SSD pen simulation](docs/weapon-detection.md) for model provenance and sample attribution.

## Run the demo

```sh
source .venv/bin/activate
python scripts/run_demo.py
```

Open [Blocka.i](http://127.0.0.1:5001). The launcher starts Flask, the face trainer and the ledger worker. It checks or starts the project MySQL server on port `3307` and the local Anvil node on port `8545`. Use `--check` to verify the database and chain connections or `--port 5002` to choose another web port.

Ctrl+C stops the app processes started by the launcher. Use `python scripts/mysql_server.py stop` to stop the project MySQL server. Logs are under `instance/private/logs/`.

### Access from a PC, tablet, or phone

Start one copy of the launcher with LAN access on a trusted local network:

```sh
python scripts/run_demo.py --lan
```

Connect the other device to the same network and open `http://<Mac LAN address>:5001` in its browser. Find the Mac’s address in **System Settings → Network → Wi-Fi → Details → TCP/IP**. The LAN option changes the Flask listener. MySQL and Anvil use their existing local listeners.

For webcam capture on another device, serve the site over HTTPS with a certificate trusted by that device and valid for the hostname or IP in the browser address:

```sh
python scripts/run_demo.py --lan --cert /path/to/certificate.pem --key /path/to/private-key.pem
```

Open `https://<certificate hostname or IP>:5001` and grant webcam access when prompted. See [browser webcam requirements](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia#privacy_and_security).

## Demonstrate the complete flow

1. Register a participant, then log in. Upload at least three different JPEG/PNG photos, each containing exactly one clear face. Maximum: 5 MB per photo, 20 photos and 25 MB per request. Exact duplicate files do not count.
2. Watch training status. A queued job needs the trainer terminal running. OpenFace produces the embeddings; only the identity matcher/classifier is fitted to your photos. The pretrained neural network is not retrained.
3. Open **Room cameras** and select **Start webcam**. Allow camera access in the browser and, if requested, in device settings. Use localhost on the Flask host or trusted HTTPS on another device.
4. Stay visible in **Room 1** for approximately 20 seconds. Click **Room 2** and stay visible for approximately 10 seconds. Leave the camera view for more than three seconds, then stop capture.
5. In another browser profile/private window, open [administrator login](http://127.0.0.1:5001/admin/login). Select the participant and date under **Room analytics**. The chart should show roughly twice as much observed time in Room 1; exact values vary with successful detections and sampling.
6. Open **Chatbot**, select the same participant and date, and choose a suggested question or type your own. The participant and date fields define the scope of each answer.
7. Show a pen to the participant's webcam while an administrator page is visible in the other profile. A detected pen opens an SSD simulation alert on the administrator page. Select **Acknowledge** or open **SSD alerts** to review the history.

Use separate browser profiles for simultaneous participant and administrator logins because tabs in one profile share the same login cookie. Only one capture session can own the demonstration webcam at a time, across accounts and pages.

## Recognition and timing

- Haar locates faces. A shared preprocessing path converts each crop to the OpenFace 96×96 input and normalizes its 128-dimensional embedding. Enrollment accepts one face per photo; live frames can recognise multiple enrolled people.
- One enrolled identity uses nearest-sample embedding distance. With two or more eligible identities, a linear `SVC(C=1)` chooses the identity and must agree with the nearest enrolled sample. A distance gate rejects unfamiliar faces in both modes. Identity always comes from recognition, not the login account.
- `MATCH_DISTANCE=0.8` is the configurable starting L2-distance threshold for normalized embeddings. It is a demonstration setting, not a measured accuracy guarantee. Lower it to reduce false matches or add more varied enrollment photos. With only three photos, lighting and pose have a substantial effect. SVC probabilities are deliberately not presented as calibrated confidence scores.
- Browser capture samples about twice per second and sends one request at a time. The server checks the session owner, random token, room generation, and increasing frame sequence. Late frames from the previous room cannot add time to the new room.
- A repeated, byte-identical frame can reuse a single recognised face result in the same capture and room generation when the model and matching threshold agree. Cache hits still record fresh observations for recognised identities. The preview draws a box and name for detected faces.
- Two consecutive matching observations confirm a visit. Confirmed visits bridge gaps of at most three seconds. Longer gaps end a visit at its last recognised frame; unconfirmed candidates add no room time. Charts do not extrapolate an active visit into the future.
- Switching rooms ends the old interval at its last sighting and requires fresh recognition. Hidden pages, navigation, logout, camera shutdown and disconnects stop capture. A missing heartbeat expires the capture lease after five seconds. On the next frame, capture action or report request, stale records are closed using their last confirmed sighting. A server restart cannot add offline time.
- Store all timestamps as UTC, including microseconds in MySQL. Display and date filters use `Europe/Dublin`, including daylight-saving transitions. Visits spanning midnight are clipped to the selected local day. Pie percentages use Room 1 plus Room 2 observed time as the denominator.

## MySQL, audit ledger and SSD alerts

MySQL is the operational query store for all existing charts and chatbot responses. Recognition, presence and weapon changes also write canonical, hash-linked audit snapshots and a durable outbox in the same SQL transaction. A separate worker anchors only hash commitments to the local development chain. Chain outages do not block camera requests.

```sh
flask --app surveillance ledger-status
flask --app surveillance ledger-verify
flask --app surveillance ledger-verify --chain
```

Verification checks stored audit hashes and their links, reconciles the latest snapshots against operational rows, and can verify mined blockchain commitments. Anvil is a single-node, resettable development blockchain; this demonstration provides tamper evidence while its history remains intact, not independent-validator guarantees. See [the full design and its sources](docs/mysql-ledger.md).

The SSD subsystem uses TensorFlow's **MobileNet V2 SSD** trained on Open Images V4 and accepts only its **Pen** class for the simulation. It samples frames in one background worker and discards results from ended or expired captures and previous room generations. Signed-in administrators receive a live prompt on visible admin pages through a three-second fetch poll. Alert history and acknowledgement are available under **SSD alerts**.

```sh
flask --app surveillance weapon-smoke
flask --app surveillance weapon-smoke --image instance/private/models/weapons/pen-test.jpg --expect Pen
```

The smoke command checks a labeled example without writing presence or alert records. Model provenance and sample attribution are in [SSD pen simulation](docs/weapon-detection.md).

## Architecture and interfaces

`surveillance/` contains the application factory, role-controlled route blueprints, database models and independent services for vision, recognition, training, presence and reporting. `migrations/` contains the initial reversible Alembic schema. `tests/` exercises the application with isolated databases and private directories. The ledger and weapon migrations follow the initial schema in one linear revision chain.

The checkout contains the source, migrations, tests, lockfiles and placeholder configuration. Keep `.env`, `instance/`, enrollment photos, trained artifacts, database exports, ledger state and TLS keys local. Webcam frames are discarded after inference. Accepted recognitions store identity, room, UTC timestamp, matching distance and model version. Confirmed intervals support duration reporting, and enrollment photographs are served only to their owner through an authenticated route.

Primary endpoints:

| Interface | Purpose |
| --- | --- |
| `/register`, `/login`, `/admin/login`, `POST /logout` | Participant registration and role-controlled sessions |
| `/enrollment`, `/cameras` | Participant pages |
| `POST /api/enrollment/photos` | Multipart `photos`; returns accepted/rejected files and queued job ID |
| `GET /api/enrollment/status`, `POST /api/enrollment/retrain` | Own enrollment/training status and retry |
| `POST /api/camera/start` | JSON room `1` or `2`; returns `capture_id`, `token`, `room`, `generation` |
| `POST /api/camera/switch` | Capture credentials plus new room; returns updated generation |
| `POST /api/camera/frame` | Multipart capture credentials, generation, increasing sequence and image `frame` |
| `POST /api/camera/stop` | Capture credentials, JSON or form beacon |
| `/admin/analytics`, `/admin/chatbot`, `/admin/alerts` | Separate administrator pages |
| `GET /api/admin/weapon-alerts` | Admin-only SSD history and latest pending simulation alert. `?pending=1` returns only the pending alert |
| `POST /api/admin/weapon-alerts/<id>/acknowledge` | Record administrator acknowledgement |
| `GET /api/admin/report` | `user_id` and ISO `day`; duration totals, visits and last sighting |
| `POST /api/admin/chat` | JSON `user_id`, `day`, `question`; answer and the same report data |

All state-changing web endpoints require a valid CSRF token. JSON/multipart requests use `X-CSRFToken`, available in the page's `csrf-token` meta element; form beacons use `csrf_token`. No API accepts an identity supplied by the browser as a recognition result.

The single trainer holds an exclusive local lock, recovers interrupted jobs, snapshots eligible enrollment images, writes an immutable artifact, and then publishes its database pointer. Failed retraining preserves the previous active model. The web process reloads the active version when it changes. A shared local state lock serializes capture and publication across processes for both supported databases. Run web and trainer on the same machine and private directory.

## Checks and maintenance

```sh
python -m pytest -q
ruff check surveillance tests scripts migrations
flask --app surveillance db check
flask --app surveillance model-smoke
flask --app surveillance weapon-smoke
flask --app surveillance ledger-verify --chain
```

Most recognition-flow tests use an explicitly isolated deterministic test double so they can verify timing and failure behavior. The real-model test checks actual OpenFace inference when weights are present. A separate Haar test verifies blank-image rejection. These checks do not replace the webcam demonstration above or establish recognition accuracy on your photographs.

The MySQL integration test is skipped unless `TEST_MYSQL_URL` points to a **dedicated, empty** `mysql+pymysql` database whose name ends in `_test`. It upgrades the schema, tests enrollment/training and subsecond reporting, then removes its test schema. Never use your demonstration database for this variable.

```sh
TEST_MYSQL_URL='mysql+pymysql://test_user:password@127.0.0.1:3307/surveillance_test' python -m pytest -m mysql -q
```

Other commands:

```sh
flask --app surveillance trainer --once
flask --app surveillance queue-training PARTICIPANT_USERNAME
flask --app surveillance reset-password USERNAME
```

If recognition is unavailable, check the enrollment job message, trainer output, and weights path. If the camera is busy in another page, stop it there or wait five seconds. A permission error requires enabling this browser's camera access in the browser/macOS settings. An interrupted upload can leave an unreferenced private file, and interrupted publication can leave an inactive artifact; neither becomes an active model or affects totals.

## Original-project references and boundaries

The [2018 tutorial](https://pyimagesearch.com/2018/09/24/opencv-face-recognition/) uses OpenFace embeddings and SVC. This rebuild uses its embedding approach with the requested Haar detector; OpenFace is an implementation inspired by FaceNet, rather than a newly trained original FaceNet network. [CMU's model documentation](https://cmusatyalab.github.io/openface/models-and-accuracies/) provides the pretrained weights. The downloader verifies the checksum published by CMU's `models/get-models.sh` over HTTPS. OpenFace weights are attributed to Carnegie Mellon University under Apache 2.0.

The demo follows the original flow from camera capture to face identification, timestamped records, and administrator queries. One browser webcam supplies both simulated rooms. The activity chatbot uses a local ChatterBot rules adapter for room time, confirmed visits, and last sightings. Answers use the same SQL reports as the Canvas 2D charts. The adapter uses fixed rules and stores no conversation history.
