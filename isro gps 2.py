import sys
import os
import time
import re
import math
import requests
import xml.etree.ElementTree as ET
import json
import base64
import traceback

from PyQt5.QtWidgets import *
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtCore import Qt, QTimer


# ==========================================
# 🛡️ CRASH CATCHER
# ==========================================
def exception_hook(exctype, value, tb):
    error_msg = "".join(traceback.format_exception(exctype, value, tb))
    print(error_msg)
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Critical)
    msg.setText("CRITICAL SYSTEM ERROR")
    msg.setDetailedText(error_msg)
    msg.exec_()
    sys.exit(1)


sys.excepthook = exception_hook


# ==========================================
# HELPER: LOAD IMAGES
# ==========================================
def load_image_as_base64(filename):
    path = os.path.join("assets", filename)
    if not os.path.exists(path): return ""
    try:
        with open(path, "rb") as image_file:
            encoded = base64.b64encode(image_file.read()).decode('utf-8')
            return f"data:image/png;base64,{encoded}"
    except:
        return ""


# ==========================================
# HELPER: PARSE OSM
# ==========================================
def parse_osm_to_geojson(osm_file_path):
    if not os.path.exists(osm_file_path): return None
    try:
        tree = ET.parse(osm_file_path)
        root = tree.getroot()
        nodes = {}
        features = []

        for node in root.findall('node'):
            nodes[node.get('id')] = (float(node.get('lon')), float(node.get('lat')))

        for way in root.findall('way'):
            coords = []
            tags = {tag.get('k'): tag.get('v') for tag in way.findall('tag')}
            for nd in way.findall('nd'):
                ref = nd.get('ref')
                if ref in nodes: coords.append(nodes[ref])

            if len(coords) > 1:
                ftype = "other"
                if 'building' in tags:
                    ftype = "building"
                elif 'highway' in tags:
                    ftype = "road"

                gtype = "Polygon" if ftype == "building" else "LineString"
                if gtype == "Polygon": coords = [coords]

                features.append({
                    "type": "Feature",
                    "geometry": {"type": gtype, "coordinates": coords},
                    "properties": {"type": ftype}
                })

        return json.dumps({"type": "FeatureCollection", "features": features})
    except:
        return None


# ==========================================
# MAIN CLASS
# ==========================================
class NavigationDashboard(QWidget):
    def __init__(self):
        super().__init__()
        self.is_tracking = False

        # PROXY KILLER
        self.session = requests.Session()
        self.session.trust_env = False

        if not os.path.exists("assets"):
            QMessageBox.critical(self, "Setup Error", "The 'assets' folder is missing!")

        # INITIAL VIEW CENTER (Just for the camera, NOT the pins)
        # This ensures you see the map graphics while waiting for GPS
        self.view_lat = 12.962322
        self.view_lon = 77.655222

        # State - All set to 0.0 initially
        self.anchor_lat = 0.0
        self.anchor_lon = 0.0
        self.indoor_start_lat = 0.0
        self.indoor_start_lon = 0.0
        self.outdoor_final_url = ""
        self.indoor_final_url = ""

        # UI
        self.setWindowTitle("ISRO GPS - Tactical Display")
        self.resize(1200, 800)
        self.setStyleSheet("background:#0f172a; color:#e2e8f0; font-family: Segoe UI;")

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Sidebar
        sidebar = QFrame()
        sidebar.setFixedWidth(320)
        sidebar.setStyleSheet("""
            QFrame { background:#0b1120; border-right:1px solid #1e293b; }
            QLabel { color: #94a3b8; font-weight: bold; }
            QLineEdit { background: #1e293b; border: 1px solid #334155; color: #fff; padding: 8px; border-radius: 4px; }
            QPushButton { background: #3b82f6; color: white; font-weight: bold; border-radius: 4px; padding: 10px; }
            QPushButton:hover { background: #2563eb; }
        """)
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(20, 30, 20, 30)
        sb.setSpacing(15)

        sb.addWidget(QLabel("OUTDOOR MODULE IP"))
        self.input_outdoor_ip = QLineEdit("10.219.223.53")
        sb.addWidget(self.input_outdoor_ip)

        sb.addWidget(QLabel("INDOOR MODULE IP"))
        self.input_indoor_ip = QLineEdit("10.219.223.218")
        sb.addWidget(self.input_indoor_ip)

        sb.addWidget(QLabel("OFFSET (Meters / Degrees)"))
        self.input_offset_dist = QLineEdit("0")
        sb.addWidget(self.input_offset_dist)
        self.input_offset_deg = QLineEdit("0")
        sb.addWidget(self.input_offset_deg)

        self.btn_start = QPushButton("INITIALIZE TRACKING")
        self.btn_start.clicked.connect(self.toggle_tracking)
        sb.addWidget(self.btn_start)

        self.lbl_status = QLabel("STANDBY")
        self.lbl_status.setAlignment(Qt.AlignCenter)
        sb.addWidget(self.lbl_status)

        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setStyleSheet(
            "background:#020617; color:#10b981; border:1px solid #1e293b; font-family: Consolas, monospace; font-size:11px;")
        sb.addWidget(self.console)

        main_layout.addWidget(sidebar)

        # Map
        self.map_view = QWebEngineView()
        self.map_view.setHtml(self.get_offline_map_html(self.view_lat, self.view_lon))
        main_layout.addWidget(self.map_view)

        # Timer
        self.outdoor_timer = QTimer()
        self.outdoor_timer.timeout.connect(self.poll_outdoor_gps)
        self.outdoor_timer.setInterval(1000)

        self.indoor_timer = QTimer()
        self.indoor_timer.timeout.connect(self.poll_indoor_imu)
        self.indoor_timer.setInterval(250)

    def log(self, msg):
        self.console.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        self.console.verticalScrollBar().setValue(self.console.verticalScrollBar().maximum())

    def format_url(self, ip):
        url = ip.strip()
        if not url.startswith("http"): url = "http://" + url
        return url.rstrip("/")

    def toggle_tracking(self):
        if not self.is_tracking:
            try:
                dist_txt = self.input_offset_dist.text()
                deg_txt = self.input_offset_deg.text()
                if not dist_txt or not deg_txt:
                    dist, deg = 0, 0
                else:
                    dist, deg = float(dist_txt), float(deg_txt)
            except:
                self.log("ERR: Invalid Offset")
                return

            self.lbl_status.setText("SEARCHING FOR GPS...")
            self.lbl_status.setStyleSheet("color:#fbbf24;")

            base_out = self.format_url(self.input_outdoor_ip.text())
            self.outdoor_final_url = base_out + "/data"
            self.indoor_final_url = self.format_url(self.input_indoor_ip.text())

            self.cached_dist = dist
            self.cached_deg = deg

            self.is_tracking = True
            self.btn_start.setText("TERMINATE")
            self.btn_start.setStyleSheet("background:#ef4444;")

            self.outdoor_timer.start()
            self.indoor_timer.start()

            # --- CHANGE: DO NOT DRAW PINS YET ---
            # self.calculate_indoor_start(dist, deg) <--- REMOVED

            self.log("System Active. Waiting for Outdoor Signal...")

        else:
            self.is_tracking = False
            self.outdoor_timer.stop()
            self.indoor_timer.stop()

            # Reset Anchors on Stop so we can restart fresh
            self.anchor_lat = 0.0
            self.anchor_lon = 0.0

            self.btn_start.setText("INITIALIZE")
            self.btn_start.setStyleSheet("background:#3b82f6;")
            self.lbl_status.setText("STANDBY")
            self.log("Stopped.")

    def poll_outdoor_gps(self):
        try:
            r = self.session.get(self.outdoor_final_url, timeout=1)
            lat_m = re.search(r"(?:Latitude|Lat|LAT)[^\d-]*([-\d\.]+)", r.text, re.IGNORECASE)
            lon_m = re.search(r"(?:Longitude|Lon|LON)[^\d-]*([-\d\.]+)", r.text, re.IGNORECASE)

            if lat_m and lon_m:
                new_lat = float(lat_m.group(1))
                new_lon = float(lon_m.group(1))

                # Update Anchor if it changes OR if it's the very first lock (0.0)
                if abs(new_lat - self.anchor_lat) > 0.000001 or self.anchor_lat == 0.0:
                    self.anchor_lat = new_lat
                    self.anchor_lon = new_lon

                    self.lbl_status.setText("GPS LOCKED")
                    self.lbl_status.setStyleSheet("color:#10b981;")

                    # Log only on first lock
                    if self.indoor_start_lat == 0.0:
                        self.log(f"GPS ACQUIRED: {new_lat}, {new_lon}")

                    # NOW we draw the pins
                    self.calculate_indoor_start(self.cached_dist, self.cached_deg)
        except:
            pass

    def calculate_indoor_start(self, dist, deg):
        # --- CRITICAL CHANGE: ABORT IF NO GPS YET ---
        if self.anchor_lat == 0.0:
            return

        alat = self.anchor_lat
        alon = self.anchor_lon

        if dist == 0:
            self.indoor_start_lat = alat
            self.indoor_start_lon = alon
        else:
            rad = math.radians(deg)
            cos_lat = math.cos(math.radians(alat))
            if abs(cos_lat) < 0.0001: cos_lat = 0.0001
            self.indoor_start_lat = alat + (dist * math.cos(rad) / 111132.0)
            self.indoor_start_lon = alon + (dist * math.sin(rad) / (111132.0 * cos_lat))

        # JS Call: Centers map on the REAL GPS location
        js = f"setStartPoint({self.indoor_start_lat}, {self.indoor_start_lon}, {alat}, {alon});"
        self.map_view.page().runJavaScript(js)

    def poll_indoor_imu(self):
        # Don't poll IMU if we don't have a start point yet
        if self.anchor_lat == 0.0:
            return

        if not self.do_imu_request(self.indoor_final_url + "/data"):
            self.do_imu_request(self.indoor_final_url)

    def do_imu_request(self, url):
        try:
            r = self.session.get(url, timeout=1.0)

            h_m = re.search(r"(?:HEADING|H)[:\s]*([\d\.]+)", r.text, re.IGNORECASE)
            n_m = re.search(r"(?:NORTH|N)[:\s]*([-\d\.]+)", r.text, re.IGNORECASE)
            e_m = re.search(r"(?:EAST|E)[:\s]*([-\d\.]+)", r.text, re.IGNORECASE)

            if n_m and e_m:
                cos_lat = math.cos(math.radians(self.indoor_start_lat))
                if abs(cos_lat) < 0.0001: cos_lat = 0.0001

                dlat = float(n_m.group(1)) / 111132.0
                dlon = float(e_m.group(1)) / (111132.0 * cos_lat)
                heading = float(h_m.group(1)) if h_m else 0.0

                js = f"updatePosition({self.indoor_start_lat + dlat}, {self.indoor_start_lon + dlon}, {heading});"
                self.map_view.page().runJavaScript(js)
                return True
            else:
                return False

        except requests.exceptions.ConnectTimeout:
            self.log("Indoor: TIMEOUT")
            return False
        except:
            return False

    def get_offline_map_html(self, lat, lon):
        try:
            with open("assets/leaflet.css", "r") as f:
                css = f.read()
            with open("assets/leaflet.js", "r") as f:
                js = f.read()
            osm_data = parse_osm_to_geojson("assets/map.osm") or "null"
            img_outdoor = load_image_as_base64("outdoor.png")
            img_start = load_image_as_base64("start.png")
            img_drone = load_image_as_base64("drone.png")
        except Exception as e:
            return f"<html><body style='background:black;color:red'><h1>ASSET ERROR</h1><p>{e}</p></body></html>"

        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8"/>
            <style>{css}</style>
            <script>{js}</script>
            <style>
                html, body, #map {{ height: 100%; margin: 0; background: #050a14; }}
                #hud {{
                    position: absolute; top: 20px; right: 20px; z-index: 1000;
                    background: rgba(5,10,20,0.85); border-left: 4px solid #3b82f6;
                    color: #e2e8f0; padding: 15px; font-family: monospace;
                    min-width: 180px;
                }}
            </style>
        </head>
        <body>
            <div id="map"></div>
            <div id="hud">
                <div>LAT: <span id="lat" style="color:#10b981">--</span></div>
                <div>LON: <span id="lon" style="color:#10b981">--</span></div>
                <div>HDG: <span id="hdg" style="color:#10b981">--</span></div>
            </div>
            <script>
                var map = L.map('map', {{zoomControl:false}}).setView([{lat}, {lon}], 19);

                var osm = {osm_data};
                if(osm) {{
                    L.geoJSON(osm, {{
                        style: function(f) {{
                            return f.properties.type === 'road' ? {{color: "#0ea5e9", weight: 2}} : 
                                   f.properties.type === 'building' ? {{color: "#1e293b", weight: 1, fillColor: "#334155", fillOpacity: 0.4}} : 
                                   {{color: "#333", weight: 1}};
                        }}
                    }}).addTo(map);
                }}

                var iconOut = L.icon({{iconUrl: '{img_outdoor}', iconSize: [25, 41], iconAnchor: [12, 41], popupAnchor: [1, -34]}});
                var iconStart = L.icon({{iconUrl: '{img_start}', iconSize: [25, 41], iconAnchor: [12, 41], popupAnchor: [1, -34]}});
                var iconDrone = L.icon({{iconUrl: '{img_drone}', iconSize: [25, 41], iconAnchor: [12, 41], popupAnchor: [1, -34]}});

                var mkOut, mkIn, mkCur, pathLine, pathCoords = [];

                function setStartPoint(ilat, ilon, olat, olon) {{
                    if(mkOut) map.removeLayer(mkOut);
                    if(mkIn) map.removeLayer(mkIn);
                    if(mkCur) map.removeLayer(mkCur);
                    if(pathLine) map.removeLayer(pathLine);
                    pathCoords = [];

                    // FORCE JUMP
                    map.setView([ilat, ilon], 20);

                    mkOut = L.marker([olat, olon], {{icon: iconOut}}).addTo(map);
                    mkIn = L.marker([ilat, ilon], {{icon: iconStart}}).addTo(map);
                    mkCur = L.marker([ilat, ilon], {{icon: iconDrone, zIndexOffset:1000}}).addTo(map);

                    L.polyline([[olat, olon], [ilat, ilon]], {{color:'#64748b', dashArray:'4,8'}}).addTo(map);
                    pathCoords.push([ilat, ilon]);
                    pathLine = L.polyline(pathCoords, {{color:'#eab308', weight:3}}).addTo(map);

                    updInfo(ilat, ilon, 0);
                }}

                function updatePosition(lat, lon, h) {{
                    if(!mkCur) return;
                    mkCur.setLatLng([lat, lon]);
                    pathCoords.push([lat, lon]);
                    pathLine.setLatLngs(pathCoords);
                    updInfo(lat, lon, h);
                }}

                function updInfo(lat, lon, h) {{
                    document.getElementById('lat').innerText = lat.toFixed(6);
                    document.getElementById('lon').innerText = lon.toFixed(6);
                    document.getElementById('hdg').innerText = parseInt(h);
                }}
            </script>
        </body>
        </html>
        """


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = NavigationDashboard()
    window.show()
    sys.exit(app.exec_())