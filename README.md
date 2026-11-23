#Datacenter

Small monitoring service for server racks. It polls servers through their BMCs
using Redfish. Collect temperatures from rack sensors and store them in database
to create temperature history. It warns when airflow looks to be blocked and
needs attention.

The service also has a simple dashboard dashboard where you can see notifications, Servers
and sensors.

The mockup servers copy and communicate via redfish while the Sensors use
their own API.

The airflow detector computes how much temperature has risen over
the last minute and if temperature have risen over 3.0 celsius and the other sensors 
that are above have not risen the detector will send notification that there is 
possible airflow blockage in the rack.

Tech stack is the following
Backend: Python, FastAPI, SQLModel and APScheduler.
Frontend: Vite, React and Typescript.
Database: SQLite

Start the mockup datacenter
```bash
python3 mockup_servers/redfish_mockup.py --sensor-base-port 8081
```

Seed the database with mockup units.
```bash
uv sync
uv run seed.py
```

Start the api
```bash 
uv run fastapi dev main.py --port 8080
```

Create rack zones for each sensor.
```bash
curl -X POST http://127.0.0.1:8080/rack/create_zone/1
```

Start the dashboard
```bash
cd dashboard
npm install
npm run dev
```
To start airflow blockage scenario.
```bash
curl -X POST http://127.0.0.1:8100/api/scenarios/rising-temperature/start \
  -H "Content-Type: application/json" \
  -d '{"servers": [1, 2, 3], "sensors": [1], "rate_c_per_min": 10, "max_rise_c": 50}'
```
To stop the scenario
```bash
curl -X POST http://127.0.0.1:8100/api/scenarios/rising-temperature/stop
```
