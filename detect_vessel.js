// Playwright loaded lazily — only in the slow path (full detection).
// Fast path (position-only) uses pure HTTP and never touches the browser.
const E=process.env.MST_EMAIL||"";
const P=process.env.MST_PASSWORD||"";
const D=process.argv[2];   // leftTheFactory date OR visited date for intermediate legs
const MMSI=process.argv[3]||"";
const LEG=process.argv[4]||"nagoya"; // which leg to detect: nagoya|zeebrugge|malmo
const DEST_COUNTRY=(process.argv[5]||"").toUpperCase();  // order destination country for route matching
const HUB_PORT=(process.argv[6]||"").toUpperCase();      // intermediate hub port (e.g. SAGUNTO, ZEEBRUGGE)
// Date the car was first seen at the NEXT hub. Optional; when present it closes
// the far end of the departure window with an observation instead of a guess.
const WINDOW_END=(process.argv[7]||"").trim();
// Name of the NEXT hub on the car's route (e.g. MALMO when tracking the
// Zeebrugge leg). On a feeder leg the correct ship is the one going there.
const NEXT_HUB=(process.argv[8]||"").toUpperCase().trim();

// ── URL component allowlists ─────────────────────────────────────────────────
// Vessel names, MMSIs and IMOs are SCRAPED from myshiptracking.com, and the MMSI
// can also arrive straight from an HTTP query parameter. Interpolating those raw
// into a URL lets the source decide where we navigate next: a name containing
// "@" reparses the authority, so "x@evil.com" turns
//   https://www.myshiptracking.com/vessels/x@evil.com-mmsi-1
// into a request to host "evil.com-mmsi-1". "/", "?", "#" and "\" similarly break
// out of the intended path. Allowlisting is used rather than escaping because
// these are identifiers with a known shape — anything outside it is not
// something we should be fetching.
// Deep-sea (Japan -> Europe) legs behave very differently from short European
// feeder hops: different departure-window direction, different plausible
// destinations. Several checks below branch on this.
const IS_FEEDER_LEG = !(LEG === "nagoya" || LEG === "yokkaichi" || LEG === "hiroshima");

function safeNum(v, maxLen){
  var s = String(v == null ? "" : v).replace(/[^0-9]/g, "");
  return s.slice(0, maxLen || 15);
}
function safeSlug(v, fallback){
  var s = String(v == null ? "" : v)
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, "-")   // collapse everything else to a hyphen
            .replace(/^-+|-+$/g, "")
            .slice(0, 80);
  return s || (fallback || "vessel");
}

// Region classification for reverse-lookup route matching.
// France/Italy/Spain can be served via EITHER Sagunto (Med ship) or Zeebrugge (Northern ship),
// so we return null for them unless HUB_PORT is known, to avoid false regional rejects.
function destRegion(country){
  if(!country) return null;
  if(/^(LITHUANIA|LATVIA|ESTONIA|FINLAND|SWEDEN|NORWAY|DENMARK|POLAND|GERMANY|BELGIUM|NETHERLANDS|UNITED KINGDOM|IRELAND|UK)$/i.test(country)) return "NORTHERN";
  // Pure Mediterranean-only destinations (never served via Zeebrugge)
  if(/^(GREECE|CYPRUS|TURKEY|LEBANON|ISRAEL|JORDAN|EGYPT|LIBYA|TUNISIA|CROATIA|SLOVENIA|MALTA)$/i.test(country)) return "MEDITERRANEAN";
  // Mixed — France/Italy/Spain/Portugal can go via Sagunto OR Zeebrugge
  // Let HUB_PORT decide below
  return null;
}
// If HUB_PORT is known, override region based on which hub the order routes through
var ORDER_REGION = destRegion(DEST_COUNTRY);
if(!ORDER_REGION && HUB_PORT){
  var NORTHERN_HUBS = ['ZEEBRUGGE','BREMERHAVEN','SOUTHAMPTON','PORTBURY','MALMO','GOTHENBURG','DRAMMEN','ANTWERP','PALDISKI','VEJLE'];
  var MED_HUBS      = ['SAGUNTO','LIVORNO','PIRAEUS','VALENCIA','LAS PALMAS'];
  if(NORTHERN_HUBS.some(function(h){ return HUB_PORT.indexOf(h) >= 0; })) ORDER_REGION = "NORTHERN";
  else if(MED_HUBS.some(function(h){ return HUB_PORT.indexOf(h) >= 0; }))  ORDER_REGION = "MEDITERRANEAN";
}
process.stderr.write("Dest: "+DEST_COUNTRY+" Hub: "+(HUB_PORT||"unknown")+" → ORDER_REGION: "+(ORDER_REGION||"any")+"\n");

// MyShipTracking port IDs — complete Toyota Europe network
var PORT_IDS = {
  // Japan loading ports
  "nagoya":       4715,
  "yokkaichi":    4716,
  "hiroshima":    4717,
  // Europe main hubs (deep-sea vessel arrives here)
  "zeebrugge":    187,    // Belgium — main hub for Nordic/Baltic/France
  "bremerhaven":  107,    // Germany
  "antwerp":      48,     // Belgium
  "southampton":  390,    // UK direct
  "portbury":     2403,   // UK direct (Bristol)
  "livorno":      275,    // Italy direct (alternates with Sagunto)
  "sagunto":      362,    // Spain/Italy/France via Sagunto
  // Nordic/Baltic distribution feeders
  "malmo":        286,    // Sweden → Paldiski feeder
  "gothenburg":   380,    // Sweden
  "paldiski":     5661,   // Estonia → Baltic states truck
  "drammen":      5130,   // Norway — 70% of all Norwegian car imports
  "piraeus":      445,    // Greece — direct Mediterranean route
  // Other
  "vejle":        2593,   // Denmark
};

// Toyota Europe carriers by leg
var CARRIERS_LEG1 = ["HIGHWAY","LEADER","ACE","TOREADOR","MORNING"]; // Japan→Europe deep-sea
var CARRIERS_LEG2 = ["HIGHWAY","LEADER","ACE","MORNING","CELTIC","SIEM","HOEGH","VIKING","ANIARA"]; // Feeder
var CARRIERS_LEG3 = ["LEADER","ACE","MORNING","SIEM","NORDANA","CELTIC","HIGHWAY"]; // Baltic feeder

var TOYOTA_CARRIERS={
  "431262000":"Hamburg Highway",
  "311995000":"Elbe Highway",
  "353100000":"Galveston Highway",
  "248910000":"Toreador",
  "432817000":"Altair Leader",
  "431816000":"Equuleus Leader",
  "432985000":"Garnet Leader",
  "431912000":"Sagittarius Leader",
  "354910000":"Adriatic Highway",
  "636022929":"Morning Claire",
  "477307600":"Morning Highway",
  "357795000":"Triton Leader",
  "636020245":"Spica Leader",
  "352006172":"Undine Highway",
  "372158000":"Marguerite Ace",
  "636022333":"Wild Rose Leader",
  "308688000":"Emerald Leader",
  "309905000":"Garnet Leader 2",
  "432716000":"Bishu Highway",
  "431323000":"Cepheus Leader",
  "432988000":"Libra Leader",
  "431946000":"Leo Leader",
  "477816600":"Danube Highway",   // K-Line EUR feeder, IMO 9388595
  "308803000":"Danube Highway",   // K-Line EUR feeder, IMO 9316309 (confirmed Zeebrugge Jul 17 → Malmö → Paldiski circuit)
  "636022937":"Orchid Leader",
};

// IMO lookup for berth verification — MMSI -> IMO
// Without IMO, shipinfo.net track API returns no data
var VESSEL_IMO = {
  "431262000":"9291347",  // Hamburg Highway
  "311995000":"9388571",  // Elbe Highway
  "353100000":"9291359",  // Galveston Highway
  "248910000":"9138338",  // Toreador
  "432817000":"9388545",  // Altair Leader
  "431816000":"9342906",  // Equuleus Leader
  "432985000":"9342894",  // Garnet Leader
  "431912000":"9388533",  // Sagittarius Leader
  "354910000":"9291361",  // Adriatic Highway
  "636022929":"9388497",  // Morning Claire
  "477307600":"9388501",  // Morning Highway
  "357795000":"9388557",  // Triton Leader
  "636020245":"9388521",  // Spica Leader
  "352006172":"9388583",  // Undine Highway
  "372158000":"9388509",  // Marguerite Ace
  "636022333":"9580907",  // Wild Rose Leader
  "308688000":"9388569",  // Emerald Leader
  "309905000":"9604936",  // Garnet Leader 2
  "432716000":"9409340",  // Bishu Highway
  "431323000":"9409352",  // Cepheus Leader
  "432988000":"9342882",  // Libra Leader
  "431946000":"9342918",  // Leo Leader
  "477816600":"9388595",  // Danube Highway (K-Line EUR)
  "308803000":"9316309",  // Danube Highway (K-Line EUR) — confirmed Zeebrugge Jul 17 → Malmö → Paldiski
  "636022937":"9985411",  // Orchid Leader (NYK/K-Line 2025) — confirmed for LT-1 May 27 Nagoya departure via Suez, arrived Zeebrugge Jul 17
};

// Map delivery location names to detection leg
// Based on actual Toyota API location names observed in DB
var LOCATION_NAME_TO_LEG = {
  "toyota city":          "nagoya",
  "zeebrugge":            "zeebrugge",
  "malmo":                "malmo",
  "malmö":                "malmo",
  "paldiski":             "paldiski",
  "bristol":              "portbury",
  "southampton":          "southampton",
  "livorno":              "livorno",
  "puerto de sagunto":    "sagunto",
  "sagunto":              "sagunto",
  "drammen":              "drammen",
  "piraeus":              "piraeus",
  "gothenburg":           "gothenburg",
  "göteborg":             "gothenburg",
};

// Zeebrugge Car Terminal (ZCT) coordinates
// Confirmed from Wild Rose Leader, Elbe Highway, Garnet Leader 2 AIS data
var ZCT_LAT_MIN = 51.295, ZCT_LAT_MAX = 51.315;
var ZCT_LON_MIN = 3.215,  ZCT_LON_MAX = 3.240;

// Malmö car terminal (Skandiahamnen) coordinates
// Confirmed from Elbe Highway, Danube Highway AIS data
var MALMO_LAT_MIN = 55.610, MALMO_LAT_MAX = 55.630;
var MALMO_LON_MIN = 12.990, MALMO_LON_MAX = 13.015;

// Known Nagoya Toyota berths — confirmed from AIS data May 2026
// NOTE: "europe" flag means EXCLUSIVELY Europe-bound. Mixed-use berths
// that can serve either Europe or other routes use europe:null.
// The berth check alone cannot confirm Europe routing for mixed berths —
// that requires the TOYOTA_CARRIERS list or route scoring to confirm.
var NAGOYA_BERTHS = [
  // E5 — primary Toyota EUROPE loading berth (confirmed: Bishu Highway,
  //   Equuleus Leader, Undine Highway etc.)
  { name:'E5', europe:true,
    latMin:35.048, latMax:35.062, lonMin:136.875, lonMax:136.892 },
  // W5 — western pier, MIXED use (confirmed: Orchid Leader briefly May 29
  //   before Europe voyage; also used for other routes)
  { name:'W5', europe:null,
    latMin:35.048, latMax:35.062, lonMin:136.848, lonMax:136.862 },
  // KINJO South / D berth — MIXED use (confirmed: Orchid Leader main load
  //   May 28-29 for Europe voyage; also serves China/domestic routes.
  //   Orchid Leader is 200×38m — wider than most PCCs — may not fit at E5)
  { name:'KINJO', europe:null,
    latMin:35.025, latMax:35.040, lonMin:136.788, lonMax:136.808 },
];

async function verifyBerth(mmsi, imo, departDate, leg) {
  try {
    var url = 'https://shipinfo.net/topos/api/vessel/track?days=60&imo='+safeNum(imo)+'&mmsi='+safeNum(mmsi);
    var resp = await fetch(url);
    var data = await resp.json();
    var points = Array.isArray(data) ? data : (data.data || data.points || []);
    var lf = new Date(departDate+'T00:00:00Z');
    var window_start = new Date(lf.getTime() - 6*86400000);
    // Only stationary points within the time window (D-6 to D, before email arrival)
    var window_pts = points.filter(function(p){
      if(!p.lat || !p.lng) return false;
      var t = new Date(p.updated);
      return t >= window_start && t <= lf && (p.speed_kn||0) <= 1;
    });

    if(leg === 'nagoya'){
      // Check each known Nagoya berth
      for(var bi=0; bi<NAGOYA_BERTHS.length; bi++){
        var b = NAGOYA_BERTHS[bi];
        var hits = window_pts.filter(function(p){
          return b.latMin <= p.lat && p.lat <= b.latMax &&
                 b.lonMin <= p.lng && p.lng <= b.lonMax;
        });
        if(hits.length > 0){
          process.stderr.write('Berth check: '+mmsi+' at '+b.name+
            ' (europe='+b.europe+'): '+hits.length+' hits\n');
          // Return berth name so caller can decide score
          return b.europe ? 'E5' : b.name;
        }
      }
      process.stderr.write('Berth check: '+mmsi+' NOT at any known Nagoya berth\n');
      return false;
    }

    // Zeebrugge and Malmo — single berth check
    var latMin, latMax, lonMin, lonMax;
    if(leg === 'zeebrugge'){
      latMin=ZCT_LAT_MIN; latMax=ZCT_LAT_MAX; lonMin=ZCT_LON_MIN; lonMax=ZCT_LON_MAX;
    } else if(leg === 'malmo'){
      latMin=MALMO_LAT_MIN; latMax=MALMO_LAT_MAX; lonMin=MALMO_LON_MIN; lonMax=MALMO_LON_MAX;
    } else {
      return null;
    }
    var hits = window_pts.filter(function(p){
      return latMin <= p.lat && p.lat <= latMax &&
             lonMin <= p.lng && p.lng <= lonMax;
    });
    process.stderr.write('Berth check ('+leg+') for '+mmsi+': '+hits.length+' hits\n');
    return hits.length > 0;
  } catch(e) {
    process.stderr.write('Berth check failed: '+e.message+'\n');
    return null;
  }
}

function getShipFinderPosition(mmsi) {
  return new Promise(function(resolve) {
    var https = require('https');
    var opts = {
      hostname: 'www.shipfinder.com',
      path: '/ship/detail/mmsi/' + mmsi,
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html'
      }
    };
    var req = https.get(opts, function(r) {
      var d = '';
      r.on('data', function(c) { d += c; });
      r.on('end', function() {
        try {
          var latM = d.match(/(\d+)-(\d+\.\d+)\s*N/);
          var lonM = d.match(/(\d+)-(\d+\.\d+)\s*E/);
          if (!latM || !lonM) { resolve(null); return; }
          var lat = parseFloat(latM[1]) + parseFloat(latM[2]) / 60;
          var lon = parseFloat(lonM[1]) + parseFloat(lonM[2]) / 60;
          var spdM = d.match(/Speed\uff1a.*?([\d\.]+)\s*kn/);
          var crsM = d.match(/Course\uff1a.*?([\d\.]+)/);
          var dstM = d.match(/Dest\uff1a.*?([A-Z][^<\|]+)/);
          var updM = d.match(/Last update\uff1a.*?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})/);
          process.stderr.write('SF: '+lat+','+lon+(updM?' @ '+updM[1]:'')+'\n');
          resolve({
            lat: lat, lon: lon,
            speed: spdM ? parseFloat(spdM[1]) : 0,
            course: crsM ? parseFloat(crsM[1]) : null,
            dest: dstM ? dstM[1].trim() : null,
            updated: updM ? updM[1] : null,
            source: 'shipfinder'
          });
        } catch(e) { resolve(null); }
      });
    });
    req.on('error', function(e) { resolve(null); });
    req.setTimeout(15000, function() { req.destroy(); resolve(null); });
  });
}

(async()=>{

// ─────────────────────────────────────────────────────────────────────────
// HELPERS — used by both fast and slow paths.
// Defined at IIFE-top scope so they're hoisted for use anywhere below.
// ─────────────────────────────────────────────────────────────────────────

// Fetch position via shipinfo.net satellite AIS (pure HTTP, no browser).
async function getShipinfoPosition(mmsi, imo){
  try {
    var url = 'https://shipinfo.net/topos/api/vessel/track?days=3&imo='+safeNum(imo||'')+'&mmsi='+safeNum(mmsi);
    var resp = await fetch(url);
    var data = await resp.json();
    var pts = Array.isArray(data) ? data : (data.data || data.points || []);
    if(pts.length === 0) return null;
    pts.sort(function(a,b){ return new Date(a.updated) - new Date(b.updated); });
    var last = pts[pts.length-1];
    if(!last.lat || !last.lng) return null;
    var ageMin = Math.round((Date.now() - new Date(last.updated).getTime())/60000);
    return {
      lat: last.lat, lon: last.lng,
      speed: last.speed_kn != null ? last.speed_kn : null,
      course: (last.course_deg != null ? last.course_deg
               : (last.heading_deg != null ? last.heading_deg : null)),
      dest: (last.destination && last.destination.trim()) ? last.destination.trim() : null,
      eta: (last.eta && last.eta.trim()) ? last.eta.trim() : null,
      ageMin: ageMin, source: 'shipinfo'
    };
  } catch(e) {
    process.stderr.write('shipinfo position fetch failed: '+e.message+'\n');
    return null;
  }
}

// Fetch MST vessel DETAIL page via plain HTTPS (no browser, no login).
// The detail page is publicly accessible — login is only needed for the port
// arrivals/departures listing. This restores dest/ETA in the fast path.
async function getMstDetailHttp(mmsi, imo, name){
  return new Promise(function(resolve){
    try {
      var https = require('https');
      var slug = safeSlug(name || TOYOTA_CARRIERS[mmsi]);
      var path = '/vessels/'+slug+'-mmsi-'+safeNum(mmsi)+(imo?('-imo-'+safeNum(imo)):'');
      var opts = {
        hostname: 'www.myshiptracking.com',
        path: path,
        headers: {
          'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
          'Accept': 'text/html,application/xhtml+xml',
          'Accept-Language': 'en-US,en;q=0.9'
        },
        timeout: 10000
      };
      var req = https.get(opts, function(res){
        if(res.statusCode !== 200){
          process.stderr.write('MST detail HTTP '+res.statusCode+'\n');
          return resolve(null);
        }
        var body = '';
        res.on('data', function(c){ body += c; });
        res.on('end', function(){
          try {
            // Collect ALL "myst-arrival-cont" h3 contents; the LAST one is the
            // next destination (first is the previous/current port).
            var dest = null;
            var arrivalRe = /<h3 class="text-truncate m-1">([\s\S]*?)<\/h3>/g;
            var matches = [];
            var m;
            while((m = arrivalRe.exec(body)) !== null){
              var text = m[1].replace(/<[^>]+>/g,'').replace(/\s+/g,' ').trim();
              if(text) matches.push(text);
            }
            if(matches.length > 0) dest = matches[matches.length - 1];

            // ETA: parse with strict patterns to avoid catching dates near
            // unrelated "eta" substrings (e.g. lowercase "eta" in <meta> tags,
            // or position-update timestamps).
            //
            // Strategy:
            //   - Case-SENSITIVE matching (no /i flag) — only matches "ETA" label
            //   - Anchor on "ETA*", "ETA:", or "Reported ETA"
            //   - Date and time captured SEPARATELY (HTML tags often split them)
            //   - Sanity check: captured date must be IN THE FUTURE
            var eta = null;
            var etaPatterns = [
              // Combined datetime patterns (preferred — date and time adjacent)
              { re: /ETA\*[\s\S]{0,300}?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})/,        time: false },
              { re: /Reported ETA[\s\S]{0,300}?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})/, time: false },
              { re: /\bETA:[\s\S]{0,300}?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})/,       time: false },
              // Date and time captured separately (HTML tags can separate them)
              { re: /ETA\*[\s\S]{0,300}?(\d{4}-\d{2}-\d{2})[\s\S]{0,80}?(\d{2}:\d{2})/,        time: true },
              { re: /Reported ETA[\s\S]{0,300}?(\d{4}-\d{2}-\d{2})[\s\S]{0,80}?(\d{2}:\d{2})/, time: true },
              // Fallback — just date, no time
              { re: /ETA\*[\s\S]{0,400}?(\d{4}-\d{2}-\d{2})/,                       time: false },
              { re: /Reported ETA[\s\S]{0,400}?(\d{4}-\d{2}-\d{2})/,                time: false },
              { re: /\bETA\b[\s\S]{0,400}?(\d{4}-\d{2}-\d{2})/,                     time: false },
            ];
            var nowMs = Date.now();
            for(var pi=0; pi<etaPatterns.length; pi++){
              var em = body.match(etaPatterns[pi].re);
              if(em){
                var candidate;
                if(etaPatterns[pi].time && em[2]){
                  candidate = em[1] + ' ' + em[2];
                } else {
                  candidate = em[1].trim();
                }
                var dtForCheck = candidate.length > 10 ? candidate : candidate + ' 00:00';
                var candidateMs = new Date(dtForCheck.replace(' ', 'T') + 'Z').getTime();
                if(isNaN(candidateMs)) continue;
                if(candidateMs > nowMs - 86400000){
                  eta = candidate;
                  process.stderr.write('MST detail ETA: matched pattern '+pi+' → '+eta+'\n');
                  break;
                } else {
                  process.stderr.write('MST detail ETA: rejected stale candidate "'+candidate
                    +'" (pattern '+pi+', '+Math.round((nowMs-candidateMs)/86400000)+'d in past)\n');
                }
              }
            }

            if(dest || eta){
              process.stderr.write('MST detail (http): dest='+dest+' eta='+eta+'\n');
              resolve({ dest: dest, eta: eta });
            } else {
              process.stderr.write('MST detail (http): no dest/eta found in body\n');
              resolve(null);
            }
          } catch(e){
            process.stderr.write('MST detail parse failed: '+e.message+'\n');
            resolve(null);
          }
        });
      });
      req.on('error', function(e){
        process.stderr.write('MST detail fetch failed: '+e.message+'\n');
        resolve(null);
      });
      req.on('timeout', function(){
        req.destroy();
        process.stderr.write('MST detail fetch timeout\n');
        resolve(null);
      });
    } catch(e){
      process.stderr.write('MST detail outer error: '+e.message+'\n');
      resolve(null);
    }
  });
}

// ─────────────────────────────────────────────────────────────────────────
// FAST PATH: Position-only mode (called by web.py to refresh vessel position).
// When MMSI is provided and D is "dummy" (no detection needed), skip Chromium
// entirely and fetch position + dest/ETA via parallel HTTP requests.
// Turns 15-25s browser refresh into ~1s HTTP fetch.
// ─────────────────────────────────────────────────────────────────────────
if(MMSI && (D === 'dummy' || !D)){
  var result = { mmsi: MMSI, matches: [{ mmsi: MMSI, vessel: TOYOTA_CARRIERS[MMSI]||"", time:"" }] };
  process.stderr.write("Fast path: position-only mode for MMSI "+MMSI+"\n");
  // Parallel fetch — both shipinfo and MST detail at the same time
  var imo = VESSEL_IMO[MMSI] || '';
  var name = TOYOTA_CARRIERS[MMSI] || '';
  var [siPos, mstDetail] = await Promise.all([
    getShipinfoPosition(MMSI, imo),
    getMstDetailHttp(MMSI, imo, name)
  ]);
  if(siPos && siPos.lat){
    process.stderr.write("shipinfo: lat="+siPos.lat+" lon="+siPos.lon+" age="+siPos.ageMin+"min\n");
    result.position = {
      lat: siPos.lat, lon: siPos.lon,
      speed: siPos.speed != null ? siPos.speed : 0,
      course: siPos.course,
      // Prefer MST's parsed dest/ETA (cleaner labels like "SG SIN PEBGA"),
      // fall back to AIS DESTINATION text from shipinfo if MST has none.
      dest: (mstDetail && mstDetail.dest) || siPos.dest || null,
      eta:  (mstDetail && mstDetail.eta)  || siPos.eta  || null,
      ageMin: siPos.ageMin,
      name: name,
      source: "shipinfo"
    };
  }
  // If shipinfo missing or stale (>3h), try ShipFinder
  if(!result.position || result.position.ageMin > 180){
    process.stderr.write("Trying ShipFinder fallback...\n");
    var sfPos = await getShipFinderPosition(MMSI);
    if(sfPos && sfPos.lat){
      process.stderr.write("ShipFinder: lat="+sfPos.lat+" lon="+sfPos.lon+"\n");
      if(!result.position || (sfPos.ageMin != null && sfPos.ageMin < result.position.ageMin)){
        result.position = {
          lat: sfPos.lat, lon: sfPos.lon,
          speed: sfPos.speed != null ? sfPos.speed : 0,
          course: sfPos.course,
          dest: (mstDetail && mstDetail.dest) || (result.position && result.position.dest) || sfPos.dest || null,
          eta:  (mstDetail && mstDetail.eta)  || (result.position && result.position.eta)  || null,
          ageMin: sfPos.ageMin,
          name: name,
          source: "shipfinder"
        };
      }
    }
  }
  process.stdout.write(JSON.stringify(result)+"\n");
  return;
}

// ─────────────────────────────────────────────────────────────────────────
// SLOW PATH: Full detection (browser scrape + reverse-lookup).
// Only runs when we don't have an MMSI yet (initial vessel detection).
// ─────────────────────────────────────────────────────────────────────────
const {chromium} = require("playwright");

// ── Chromium sandbox ─────────────────────────────────────────────────────────
// This browser visits a third-party site (myshiptracking.com) that we do not
// control, on demand, from an unauthenticated HTTP request. That makes it the
// most exposed component in the whole system: a renderer bug on that site is
// remote code execution inside this container.
//
// '--no-sandbox' removes the mitigation that keeps such a bug contained. It was
// here because Chromium refuses to sandbox when running as root, and the
// container ran everything as root. web.py now drops to an unprivileged user
// (SCRAPER_USER) before spawning this script, so the sandbox can stay on.
//
// Playwright's own guidance for exactly this case:
//   "For web scraping or crawling, we recommend to create a separate user
//    inside the Docker container and use the seccomp profile."
//
// Escape hatch: if Chromium fails to start in your environment (the usual cause
// is Docker's default seccomp profile blocking user-namespace creation, which
// the profile in seccomp_profile.json fixes), set CHROMIUM_NO_SANDBOX=1 on the
// container to restore the old behaviour without a rebuild. That trades the
// sandbox back away, so treat it as temporary.
const SANDBOX_OFF = process.env.CHROMIUM_NO_SANDBOX === "1";
if (SANDBOX_OFF) {
  process.stderr.write("WARNING: Chromium sandbox disabled via CHROMIUM_NO_SANDBOX=1 — " +
                       "a renderer exploit on a scraped page becomes code execution in this container.\n");
}
const br=await chromium.launch({
  headless: true,
  args: [
    '--disable-dev-shm-usage',  // use /tmp not /dev/shm (which is small in Docker)
    '--disable-gpu',
    '--disable-blink-features=AutomationControlled',
  ].concat(SANDBOX_OFF ? ['--no-sandbox', '--disable-setuid-sandbox'] : [])
});
const pg=await (await br.newContext()).newPage();
try{
// Login
await pg.goto("https://www.myshiptracking.com/",{timeout:30000,waitUntil:"domcontentloaded"});
await pg.waitForTimeout(4000);
for(var i=0;i<3;i++){
  try{await pg.click("button:has-text(\"Accept all\")",{timeout:500});}catch(e){}
  try{await pg.click("button:has-text(\"Agree\")",{timeout:500});}catch(e){}
  try{await pg.click("[id*=accept]",{timeout:500});}catch(e){}
}
await pg.waitForTimeout(1000);
await pg.evaluate(function(){open_login_window();});
await pg.waitForTimeout(2000);
await pg.evaluate(function(creds){
  var em=document.querySelector("input[name=email]");
  var pw=document.querySelector("input[name=password]");
  if(em){em.style.cssText="display:block!important;visibility:visible!important;opacity:1!important";em.value=creds[0];em.dispatchEvent(new Event("input",{bubbles:true}));}
  if(pw){pw.style.cssText="display:block!important;visibility:visible!important;opacity:1!important";pw.value=creds[1];pw.dispatchEvent(new Event("input",{bubbles:true}));}
},[E,P]);
await pg.waitForTimeout(500);
await pg.click("#submit_login",{timeout:5000,force:true});
await pg.waitForTimeout(5000);
process.stderr.write("Logged in: "+(pg.url().indexOf("/login")===-1)+"\n");

var result={};

if(MMSI){
  // Position-only mode
  result.mmsi=MMSI;
  result.matches=[{mmsi:MMSI,vessel:TOYOTA_CARRIERS[MMSI]||"",time:""}];
} else {
  // Detect vessel from port departures
  var pid = Object.prototype.hasOwnProperty.call(PORT_IDS, LEG) ? PORT_IDS[LEG] : PORT_IDS["nagoya"];
  var carriers = LEG === "nagoya"     ? CARRIERS_LEG1 :
                 LEG === "zeebrugge" ? CARRIERS_LEG2 :
                 LEG === "malmo"     ? CARRIERS_LEG3 : CARRIERS_LEG1;

  // Departure window logic (calibrated from observed cases):
  // - Toyota's "left the factory" notification arrives ~1-7 days AFTER the ship
  //   actually sailed (logistics delay between car loading and status update).
  //   Observed gaps: Bishu Highway/Triton Leader = 5 days (LT-1, FR-3 cases).
  // - Therefore the ship MUST have departed BEFORE the user-entered date.
  // - Days AFTER the entered date are logically wrong (the ship can't depart after
  //   the email announcing its departure) — searching them only adds noise like
  //   same-day arrivals (e.g. Vela Leader at Nagoya for loading, not departing).
  // Window: D-6 to D-1 — 6 days, all strictly before the recorded date.
  // Covers the observed 5-day gap with a 1-day safety margin.
  //
  // THAT REASONING ONLY HOLDS FOR THE NAGOYA LEG. There, D is the date of the
  // "left the factory" email, which arrives AFTER the ship sailed, so searching
  // backwards is right.
  //
  // On a feeder leg (Zeebrugge->Malmo, Malmo->Paldiski) D means the opposite:
  // it is when we observed the car AT that hub. The ship departs at or AFTER
  // that moment, never 6 days before it. Using the backward window there
  // searched a period the car had not even reached the hub in — which is why
  // Zeebrugge departures for LT-1 returned Prima Viking / Morning Lucy /
  // Hoegh Trooper and not Danube Highway or Elbe Highway: those sailed after
  // the window closed, so they were never in the 50 rows to begin with.
  //
  // Feeder window straddles D instead:
  //   -2 days: our observation can lag the real arrival, since logins are
  //            periodic and the hub date is "first time we SAW the car here".
  //   +8 days: cars wait at the hub for the next feeder sailing; the observed
  //            Zeebrugge->Malmo cycle runs about a week.
  var lf=new Date(D+"T00:00:00Z");
  var isDeepSea = !IS_FEEDER_LEG;
  var backDays    = isDeepSea ? 6 : 2;
  var forwardDays = isDeepSea ? 1 : -8;   // negative = search forward past D
  var start=Math.floor((lf.getTime()-backDays*86400000)/1000);
  var end  =Math.floor((lf.getTime()-forwardDays*86400000)/1000);
  var windowSource = isDeepSea ? "deep-sea, fixed span" : "feeder, fixed span";

  // If the caller knows when the car turned up at the NEXT hub, the sailing is
  // bracketed by two real observations rather than guessed at: it cannot have
  // left before the car was here, and cannot still have been at sea after the
  // car appeared there. That is both tighter and safer than any +/-N default —
  // a fixed span is only ever right by luck, and when the two hub observations
  // are far apart (infrequent logins) a short window misses the sailing
  // entirely, which is exactly how Danube/Elbe Highway were never in the list.
  if(!isDeepSea && WINDOW_END && /^\d{4}-\d{2}-\d{2}$/.test(WINDOW_END)){
    var we = new Date(WINDOW_END+"T00:00:00Z");
    if(!isNaN(we) && we.getTime() >= lf.getTime()){
      end = Math.floor((we.getTime()+1*86400000)/1000);   // +1d for observation lag

      // Work BACKWARDS from the next-hub arrival, not forwards from this hub.
      // Arriving at Zeebrugge only says when the car got there — it can wait days
      // for the next sailing, so that date is a weak lower bound and anchoring on
      // it produces a window that is both too wide and centred in the wrong place.
      // The Malmo arrival is the tight signal: the voyage ENDS there, so the
      // departure sits roughly one sea-transit earlier. Zeebrugge still bounds
      // it — the ship cannot have left before the car arrived — so take whichever
      // lower bound is later.
      // How far back from the arrival to look, per departure port. These are
      // SEA transit times, not door-to-door: Zeebrugge->Malmo is about two days
      // of actual sailing, and any extra elapsed time is the car waiting on the
      // quay — which the window must not try to cover, because the ship was not
      // at sea then. Padding these "just in case" is what pulled in 50 unrelated
      // departures and let a ship bound for India score highest.
      var TRANSIT_BY_LEG = {
        zeebrugge:   3,   // -> Malmo / Baltic feeder, ~2 days at sea
        malmo:       3,   // -> Paldiski, overnight
        gothenburg:  3,
        drammen:     3,
        southampton: 3,
        portbury:    3,
        bremerhaven: 3,
        sagunto:     5,   // Mediterranean legs run longer
        livorno:     5,
        piraeus:     5,
      };
      var maxTransitDays = parseInt(
        process.env.FEEDER_TRANSIT_DAYS || TRANSIT_BY_LEG[LEG] || 3, 10);
      var backstop = Math.floor((we.getTime() - maxTransitDays*86400000)/1000);
      if(backstop > start) start = backstop;
      windowSource = "feeder, back from next-hub arrival "+WINDOW_END+
                     " (max "+maxTransitDays+"d transit, floored at this hub's arrival)";
    }
  }
  process.stderr.write("Departure window for "+LEG+" ("+windowSource+"): "+
    new Date(start*1000).toISOString().slice(0,10)+" .. "+
    new Date(end*1000).toISOString().slice(0,10)+" (anchor D="+D+")\n");
  // ── Read the port listing, ALL pages ────────────────────────────────────────
  // MyShipTracking paginates this table at 50 rows and ignores the &pp= hint.
  // Only page 1 used to be read, so the search was silently capped at the 50
  // most recent departures in the window — Zeebrugge alone does far more than
  // that in a few days. Elbe Highway (Zeebrugge, 2026-07-25 14:15) sat on
  // page 4 and was never seen, which no amount of tuning the date window could
  // have fixed: the ship was in range the whole time, just past the cap.
  //
  // sort=TIME makes the ordering deterministic so paging is stable.
  var PAGE_SIZE = 50;
  var MAX_PAGES = parseInt(process.env.MST_MAX_PAGES || "12", 10);
  var scrapeRows = function(){
    var tr=document.querySelectorAll("table tbody tr"),out=[];
    for(var i=0;i<tr.length;i++){
      var td=tr[i].querySelectorAll("td");
      var links=tr[i].querySelectorAll("a[href*=vessels]");
      var mmsi="";
      for(var j=0;j<links.length;j++){var m=links[j].href.match(/mmsi-(\d+)/);if(m){mmsi=m[1];break;}}
      var vessel=td[4]?td[4].innerText.trim().replace(/\s*\[.*\]/,""):"";
      var time=td[2]?td[2].innerText.trim():"";
      if(vessel)out.push({time:time,vessel:vessel,mmsi:mmsi});
    }
    return out;
  };

  var rows=[], seenRow={};
  for(var page=1; page<=MAX_PAGES; page++){
    var u="https://www.myshiptracking.com/ports-arrivals-departures/?sort=TIME&page="+page+
          "&pid="+safeNum(pid)+"&type=2&time="+safeNum(start,12)+"_"+safeNum(end,12);
    if(page===1) process.stderr.write("Port "+LEG+" (pid:"+pid+") URL: "+u+"\n");
    try{
      await pg.goto(u,{timeout:30000,waitUntil:"domcontentloaded"});
      await pg.waitForTimeout(page===1?6000:2500);
    }catch(e){
      process.stderr.write("page "+page+" failed to load: "+e.message+"\n");
      break;
    }
    var pageRows = await pg.evaluate(scrapeRows);
    // Duplicate rows mean we have run off the end and the site is re-serving the
    // last page — stop rather than loop to MAX_PAGES pointlessly.
    var added = 0;
    for(var pr=0; pr<pageRows.length; pr++){
      var key = pageRows[pr].mmsi+"|"+pageRows[pr].time;
      if(seenRow[key]) continue;
      seenRow[key] = true; rows.push(pageRows[pr]); added++;
    }
    process.stderr.write("  page "+page+": "+pageRows.length+" rows ("+added+" new, "+rows.length+" total)\n");
    if(pageRows.length === 0 || added === 0) break;
    if(pageRows.length < PAGE_SIZE) break;   // short page = last page
    if(page === MAX_PAGES){
      process.stderr.write("WARNING: hit MST_MAX_PAGES="+MAX_PAGES+"; there may be more "+
        "departures in this window that were not examined.\n");
    }
  }

  var C=carriers;
  var knownMMSIs=Object.keys(TOYOTA_CARRIERS);
  // Primary filter: carrier name keywords OR known MMSI
  // Fallback for nagoya/zeebrugge: also include any RoRo vessel (berth will verify)
  var matches=rows.filter(function(r){
    if(knownMMSIs.indexOf(r.mmsi)>=0) return true; // known Toyota carrier
    if(C.some(function(c){return r.vessel.toUpperCase().indexOf(c)>=0;})) return true;
    // For berth-verified legs: include all large vessels as candidates
    // (berth check will reject non-Toyota vessels)
    if(LEG==='nagoya'||LEG==='zeebrugge'||LEG==='malmo'){
      var roro=['RO-RO','RORO','CAR CARRIER','VEHICLE','PCC'];
      // We can't check vessel type from port list, so keep name filter
      // but add common Toyota carrier patterns
      if(/\b(ACE|LEADER|HIGHWAY|MORNING|TOREADOR|TRIUMPH|SPIRIT|BRAVE)\b/i.test(r.vessel)) return true;
    }
    return false;
  });
  process.stderr.write('Name filter: '+matches.length+' matches from '+rows.length+' total\n');

  // Two different date windows both returning exactly 50 rows is a page-size cap,
  // not a coincidence — the listing is paginated and we only ever read page one.
  // When that happens the search is silently truncated: the right ship can be
  // sitting at row 51 and no amount of fixing the window will surface it. Say so
  // loudly, and dump what we DID see so the gap is visible rather than inferred.
  var times = rows.map(function(r){ return r.time; }).filter(Boolean).sort();
  process.stderr.write('Rows span: '+(times[0]||'?')+'  ..  '+(times[times.length-1]||'?')+'\n');
  if(matches.length === 0){
    // No candidates is the case where you need to see the raw list to tell
    // "wrong window" from "right window, name filter too strict".
    process.stderr.write('Vessels seen ('+rows.length+'): '+
      rows.map(function(r){return r.vessel;}).join(', ')+'\n');
  }

  // Always score ALL matches by Europe port history — even single match
  // This prevents false positives from carriers going to Americas/Asia/Pacific
  var EUROPE=["ZEEBRUGGE","BREMERHAVEN","SOUTHAMPTON","ANTWERP","ROTTERDAM",
              "MALMO","PALDISKI","PORTBURY","SAGUNTO","GOTHENBURG","LIVORNO",
              "PIRAEUS","DRAMMEN","ONNAING","VALENCIA"];
  if(matches.length > 0){
    process.stderr.write("Scoring "+matches.length+" match(es) by Europe port history...\n");
    for(var mi=0;mi<matches.length;mi++){
      var m=matches[mi];
      if(!m.mmsi){ m.europeScore=0; continue; }
      try{
        // Early check: if the consolidated position dest (from MST detail / shipinfo) is
        // an off-route Japan/Pacific port, penalise immediately before any further scoring.
        var OFF_ROUTE_DEST_EARLY=[
          'LOS ANGELES','LONG BEACH','BALTIMORE','BRUNSWICK','JACKSONVILLE',
          'SYDNEY','MELBOURNE','AUCKLAND','FREMANTLE',
          'DURBAN','MOMBASA','DAR ES SALAAM',
          'YOKOHAMA','TOKYO','OSAKA','KOBE','HITACHI','KASHIMA',
          'BUSAN','GUANGZHOU','TIANJIN','SHANGHAI','HONG KONG','KAOHSIUNG',
          'HAKATA','NIIGATA','SENDAI','MURORAN',
        ];
        var knownDest = (result && result.position && result.position.dest) ? result.position.dest.toUpperCase() : '';
        if(knownDest && OFF_ROUTE_DEST_EARLY.some(function(d){ return knownDest.indexOf(d) >= 0; })){
          process.stderr.write(m.vessel+": MST/shipinfo dest="+knownDest+" is off-route, penalizing -20\n");
          if(!m.europeScore) m.europeScore = 0;
          m.europeScore -= 20;
        }
        // Feeder legs know where the car is going: Zeebrugge -> Malmo -> Paldiski
        // are all short intra-European hops. A ship on that run is bound for a
        // European port, full stop. The generic OFF_ROUTE lists are blocklists
        // built for the deep-sea leg and contain no Indian-subcontinent ports,
        // so Hoegh Trooper — sitting at the Zeebrugge berth but sailing for
        // ENNORE, India — passed every check and got picked as LT-1's carrier.
        //
        // For feeders an allowlist is both stronger and simpler: if we know the
        // destination and it is not European, this is not our ship. Deep-sea is
        // deliberately excluded, since a Japan->Europe rotation legitimately
        // calls at Singapore, Colombo, Suez and similar en route.
        if(IS_FEEDER_LEG && knownDest){
          var EURO_DEST_OK = EUROPE.concat([
            'ZEEBRUGGE','MALMO','MALMÖ','PALDISKI','GOTHENBURG','GOTEBORG',
            'DRAMMEN','BREMERHAVEN','ANTWERP','ROTTERDAM','SOUTHAMPTON',
            'PORTBURY','BRISTOL','SAGUNTO','LIVORNO','PIRAEUS','VEJLE',
            'HANKO','KOTKA','RIGA','KLAIPEDA','TALLINN','HELSINKI','OSLO',
            'COPENHAGEN','HAMBURG','CUXHAVEN','AMSTERDAM','VLISSINGEN',
          ]);
          var destLooksEuropean = EURO_DEST_OK.some(function(p){ return knownDest.indexOf(p) >= 0; });
          if(!destLooksEuropean){
            process.stderr.write(m.vessel+": dest="+knownDest+" is not a European port and this is a "+
              "feeder leg ("+LEG+") — hard reject -50\n");
            if(!m.europeScore) m.europeScore = 0;
            m.europeScore -= 50;
          }
        }
        var vurl="https://www.myshiptracking.com/vessels/"+
                 safeSlug(m.vessel)+"-mmsi-"+safeNum(m.mmsi);
        await pg.goto(vurl,{timeout:20000,waitUntil:"domcontentloaded"});
        await pg.waitForTimeout(3000);
        var vtext=await pg.textContent("body");
        m.europeScore=EUROPE.filter(function(p){return vtext.toUpperCase().includes(p);}).length;

        // Log what the page looks like around "destination" so the pattern used
        // by the next-hub tie-breaker can be verified against the real page. An
        // earlier version guessed at a DESTINATION: field that turned out never
        // to match, and the whole check ran on a fallback that penalised every
        // candidate equally. This just prints — no scoring, no assumptions.
        // ── Last Trips: the actual voyages this ship has run ────────────────
        // The vessel page has a "Last Trips" table with columns
        //   Origin | Departure | Destination | Arrival | Distance
        // Every row is an OBSERVED voyage: real departure and arrival ports
        // with real timestamps. If the top row is "this hub -> next hub" on
        // dates that match our window, that IS our ship — no AIS destination
        // guessing needed.
        //
        // The other checks below (europeScore, page-destination string, live
        // AIS destination) only see what the ship is currently declaring, which
        // for a berth-idling vessel is often the previous port or a stale
        // rotation. Last Trips shows what actually happened.
        try {
          // Give the page a moment for the trips table to hydrate — it lives
          // under a heading that may render before its data arrives. Ignore the
          // timeout; the fallback that follows still works if the selector
          // never appears.
          try { await pg.waitForSelector('table', { timeout: 5000 }); } catch(_){}

          // Extract trips by SCANNING EVERY TABLE for header cells that match
          // Origin / Departure / Destination / Arrival. The previous version
          // walked DOM siblings from a "Last Trips" heading, which failed
          // silently when the heading and table lived in different branches of
          // the layout — the exact case here, since /^Last Trips/ is a card
          // header and the table sits several parents away.
          //
          // Also dump some diagnostics so this never fails silently again:
          //   trips_dbg tables=N headers=[...]  → what tables were considered
          //   trips_dbg matched cols=[...]      → which one won and its headers
          var probe = await pg.evaluate(function(){
            // Match must be STRICT: the four header cells must appear as
            // separate <th>s in ONE row, and the values must equal the labels
            // (not merely contain them as substrings). The previous, looser
            // check happily matched a Port Calls table whose header ran
            // "Port | Arrival | Departure | Time in port" — "ORIGIN" was
            // nowhere in it, but "ARRIVAL" and "DEPARTURE" appeared and the
            // scan settled for that. Elbe Highway winning was pure luck: the
            // first data row was Malmo/Malmo/2026-07-27, columns 1 and 3 both
            // read "MALMO", and my next-hub check passed on a coincidence.
            var HEADERS_WANT = ['ORIGIN','DEPARTURE','DESTINATION','ARRIVAL'];
            var tables = document.querySelectorAll('table');
            var seen = [], picked = null, colIx = null;
            for(var ti=0; ti<tables.length; ti++){
              var tb = tables[ti];
              // Consider each row of the header separately, and only <th> — a
              // <td> in the header area is data, not a label.
              var rows = tb.querySelectorAll('thead tr, tr');
              var rowHdrs = [];
              for(var ri=0; ri<Math.min(rows.length, 3); ri++){
                var ths = rows[ri].querySelectorAll('th');
                if(!ths.length) continue;
                var hdr = [];
                for(var hi=0; hi<ths.length; hi++) hdr.push((ths[hi].innerText||'').trim().toUpperCase());
                rowHdrs.push(hdr);
              }
              // Cheap summary for diagnostics.
              seen.push(rowHdrs.map(function(h){return h.join('|');}).join(' // ') || '(no <th> row)');
              for(var rh=0; rh<rowHdrs.length; rh++){
                var hdr2 = rowHdrs[rh];
                var ix = {};
                HEADERS_WANT.forEach(function(w){
                  for(var k=0; k<hdr2.length; k++){
                    // Strict: exact label match. Allows an extra column like
                    // "Origin (port)" via startsWith, but not "TIME IN PORT"
                    // matching "PORT".
                    if(hdr2[k] === w || hdr2[k].indexOf(w+' ') === 0){ ix[w]=k; break; }
                  }
                });
                if(ix.ORIGIN!=null && ix.DEPARTURE!=null && ix.DESTINATION!=null && ix.ARRIVAL!=null){
                  picked = tb; colIx = ix; break;
                }
              }
              if(picked) break;
            }
            var trips = [];
            if(picked){
              var rows = picked.querySelectorAll('tbody tr');
              if(!rows.length) rows = picked.querySelectorAll('tr');
              for(var ri=0; ri<rows.length; ri++){
                var cells = rows[ri].querySelectorAll('td');
                if(cells.length < 4) continue;
                var val = function(i){ return ((cells[i]&&cells[i].innerText)||'').replace(/\s+/g,' ').trim(); };
                var origin = val(colIx.ORIGIN);
                if(!origin || /origin/i.test(origin)) continue; // skip header row when tbody is missing
                trips.push({
                  origin: origin, depart: val(colIx.DEPARTURE),
                  dest: val(colIx.DESTINATION), arrive: val(colIx.ARRIVAL),
                });
              }
            }
            return { tables: seen.length, headers: seen.slice(0,6), matched: !!picked,
                     colIx: colIx, trips: trips.slice(0, 8) };
          });
          process.stderr.write(m.vessel+": trips_dbg tables="+probe.tables+
            " headers="+JSON.stringify(probe.headers)+
            " matched="+probe.matched+(probe.colIx?" cols="+JSON.stringify(probe.colIx):"")+
            " rows="+probe.trips.length+"\n");
          var trips = probe.trips;
          if(trips && trips.length){
            m.lastTrips = trips.slice(0, 5);
            process.stderr.write(m.vessel+": last trips (top "+m.lastTrips.length+"): "+
              m.lastTrips.map(function(t){ return t.origin+"->"+t.dest+" "+t.depart; }).join(" | ")+"\n");
          } else {
            process.stderr.write(m.vessel+": no Last Trips table found\n");
          }

          // Decisive match: any recent trip whose origin matches THIS leg's hub
          // and destination matches the NEXT hub, on dates inside our window.
          // A voyage that already happened is not a guess.
          if(NEXT_HUB && trips && trips.length && HUB_PORT){
            var hubUp = HUB_PORT.toUpperCase();
            for(var ti=0; ti<trips.length; ti++){
              var tr = trips[ti];
              var oU = (tr.origin||"").toUpperCase();
              var dU = (tr.dest||"").toUpperCase();
              if(oU.indexOf(hubUp) < 0 || dU.indexOf(NEXT_HUB) < 0) continue;
              // Origin/destination line up. Check the departure date is inside
              // our window — if it is, this ship literally did the trip.
              var td = Date.parse((tr.depart||"").replace(" ", "T")+"Z");
              if(!isFinite(td)) continue;
              if(td >= (start*1000) - 12*3600000 && td <= (end*1000) + 12*3600000){
                process.stderr.write(m.vessel+": Last Trips row proves "+hubUp+"->"+NEXT_HUB+
                  " on "+tr.depart+" — this IS the ship, +100\n");
                m.europeScore += 100;
                m.provenCarrier = true;
                m.provenDeparture = tr.depart;
                break;
              }
            }
          }
        } catch(tripsErr){
          process.stderr.write(m.vessel+": Last Trips lookup failed: "+tripsErr.message+"\n");
        }

        // Read the destination from MyShipTracking's vessel page. Layout is:
        //   DESTINATION  ARRIVAL  DISTANCE  <PORT>  <YEAR>
        // Kept as a soft signal for cases where Last Trips is empty or the
        // ship has not yet made the observed run — never load-bearing on its
        // own, because that AIS field lags heavily.
        var VU = vtext.toUpperCase();
        var pageDest = '';
        var dm = VU.match(/DESTINATION\s+ARRIVAL\s+DISTANCE\s+([A-Z][A-Z0-9 .'\/-]{2,40})/);
        if(dm){
          var raw = dm[1].trim();
          // Cut at the next standalone number (the year that follows the port).
          var cut = raw.match(/^([A-Z][A-Z .'\/-]+?)(?=\s+\d{4}\b|\s+---)/);
          pageDest = (cut ? cut[1] : raw.split(/\s+\d/)[0]).trim();
        }
        m.pageDest = pageDest;
        process.stderr.write(m.vessel+": page destination = "+(pageDest || "(none / ---)")+"\n");
        if(NEXT_HUB && pageDest){
          if(pageDest.indexOf(NEXT_HUB) >= 0){
            process.stderr.write(m.vessel+": destination IS the next hub ("+NEXT_HUB+") +30\n");
            m.europeScore += 30; m.nextHubMatch = true;
          } else if(IS_FEEDER_LEG){
            process.stderr.write(m.vessel+": destination "+pageDest+" is not the next hub ("+
                                 NEXT_HUB+") -5\n");
            m.europeScore -= 5;   // soft, since AIS destination fields lag heavily
          }
        }
        // Extract IMO from vessel page while we have it open (needed for berth verification)
        var imoMatch=vtext.match(/IMO[:\s#]*(\d{7})/i);
        if(imoMatch) { m.imo=imoMatch[1]; process.stderr.write(m.vessel+": IMO="+m.imo+"\n"); }
        // Also check last departure port — if vessel loaded at Kobe/Yokohama/etc instead of
        // Nagoya, it's on a different route. Check for "ATD" (actual time of departure) from non-Nagoya
        var lastDeptMatch=vtext.match(/ATD[^\n]*\n[^\n]*?(KOBE|YOKOHAMA|OSAKA|HIROSHIMA|NAGOYA)/i);
        if(lastDeptMatch && lastDeptMatch[1].toUpperCase() !== 'NAGOYA'){
          process.stderr.write(m.vessel+": last departure from "+lastDeptMatch[1]+" not Nagoya, penalizing -15\n");
          m.europeScore -= 15;
        }
        // Check live destination — Singapore/Suez ARE normal stops on Europe route.
        // Only penalise destinations that prove the vessel is on a non-Europe rotation:
        // Americas, Pacific, Australia, or returning to Japan from Europe.
        var OFF_ROUTE_DEST=[
          'LOS ANGELES','LONG BEACH','BALTIMORE','BRUNSWICK','JACKSONVILLE',
          'SYDNEY','MELBOURNE','AUCKLAND','FREMANTLE',
          'DURBAN','MOMBASA','DAR ES SALAAM',
          'YOKOHAMA','TOKYO','OSAKA','KOBE','HITACHI','KASHIMA',  // Japan ports (not on Europe route)
          'BUSAN','GUANGZHOU','TIANJIN','SHANGHAI','HONG KONG','KAOHSIUNG',
          'HAKATA','NIIGATA','SENDAI','MURORAN',  // other Japan ports
        ];
        // Singapore, Port Klang, Colombo, Suez ARE on the Europe route — never penalise these.
        try {
          var liveApi="https://www.myshiptracking.com/requests/vesselsonmaptempTTT.php?type=json&minlat=-90&maxlat=90&minlon=-180&maxlon=180&zoom=2&selid="+safeNum(m.mmsi)+"&seltype=0&timecode=-1&filters=%7B%7D";
          var liveResp=await pg.evaluate(async function(url){var r=await fetch(url);return await r.text();},liveApi);
          var liveDestMatch=liveResp.match(m.mmsi+"\t[^\t]+\t[\d\.]+\t[\d\.]+\t[\d\.]+\t[\d\.]+\t[\d]+\t[\d]+\t[\d]+\t[\d]+\t\t[\d]+\t([A-Z>][^\n\t]*)");
          if(liveDestMatch){
            var liveDest=liveDestMatch[1].trim().replace(/^>/,"").toUpperCase();
            process.stderr.write(m.vessel+": live dest="+liveDest+"\n");

            // On a feeder leg we know the destination: it is the next hub on the
            // car's own route. That is a far sharper signal than europeScore,
            // which only counts how many European port names appear on the
            // vessel's page — so a ship idling at Bremerhaven scores well simply
            // for being in Europe. Triton Leader (europeScore 5, at Bremerhaven)
            // outranked Elbe Highway (3, bound for Malmo) on exactly that basis,
            // despite Bremerhaven being nowhere on this car's route.
            if(NEXT_HUB && liveDest.indexOf(NEXT_HUB) >= 0){
              process.stderr.write(m.vessel+": destination matches the next hub ("+NEXT_HUB+
                                   ") — this is the leg we are tracking, +30\n");
              m.europeScore += 30;
              m.nextHubMatch = true;
            } else if(NEXT_HUB && liveDest && IS_FEEDER_LEG){
              // A European port that is not this car's next hub means the ship is
              // on a different rotation. Not fatal — AIS destination fields are
              // often stale or free-text — but it should not outrank a match.
              process.stderr.write(m.vessel+": dest "+liveDest+" is not the next hub ("+
                                   NEXT_HUB+"), -10\n");
              m.europeScore -= 10;
            }
            var isOffRoute=OFF_ROUTE_DEST.some(function(d){return liveDest.indexOf(d)>=0;});
            if(isOffRoute){
              process.stderr.write(m.vessel+": off-route destination ("+liveDest+"), penalizing -20\n");
              m.europeScore -= 20;
            }
            // Bonus: if already heading to a known Europe-rotation port, it's definitely the right ship
            // This list includes:
            //   - Northern Europe terminals (Zeebrugge, Bremerhaven, etc.)
            //   - Mediterranean terminals where Toyota Europe receives cars (Sagunto, Livorno, Piraeus)
            //   - Eastern Med/Turkey terminals (Derince — K-Line uses for Black Sea/Turkey distribution)
            //   - Levant terminals (Limassol, Beirut, Iskenderun, Latakia — for Med rotation PCCs)
            //   - Canaries (Las Palmas — common rotation stop)
            //   - Transit waypoints (Singapore, Suez, Port Said)
            // All of these mean "vessel is heading INTO the Europe-bound rotation".
            var EUROPE_DEST=['ZEEBRUGGE','BREMERHAVEN','SOUTHAMPTON','PORTBURY',
                             'SAGUNTO','LIVORNO','MALMO','GOTHENBURG','PIRAEUS','DRAMMEN',
                             'ANTWERP','ROTTERDAM','SUEZ','PORT SAID',
                             'DERINCE','ISKENDERUN','LIMASSOL','BEIRUT','LATAKIA',  // Eastern Med
                             'LAS PALMAS','CIVITAVECCHIA','GENOA','BARCELONA','VALENCIA',
                             'KOPER','TRIESTE','RIJEKA','VENICE',                    // Adriatic
                             'PALDISKI','HANKO','KOTKA','HELSINKI','KLAIPEDA','RIGA', // Baltic
                             'HAMBURG','WILHELMSHAVEN','EMDEN'];                       // North Sea
            var isEuropeDest=EUROPE_DEST.some(function(d){return liveDest.indexOf(d)>=0;});
            if(isEuropeDest){
              process.stderr.write(m.vessel+": heading to Europe ("+liveDest+"), bonus +5\n");
              m.europeScore += 5;
            }
            m.liveDest = liveDest;
          }
        } catch(e) { process.stderr.write("Live dest check failed for "+m.vessel+": "+e.message+"\n"); }

        // GEOGRAPHIC SANITY CHECK — current position proves route direction.
        // A Europe-bound ship from Nagoya heads SOUTH/SOUTHWEST (toward Singapore).
        // If it is east of ~141E, or north of Japan, or in the Americas/E.Pacific,
        // it is physically NOT on the Europe voyage this rotation — hard reject.
        // (This catches ships like Equuleus Leader that loaded at E5 but then
        //  departed on a Pacific rotation instead of sailing to Europe.)
        try {
          var posData = await getShipinfoPosition(m.mmsi, VESSEL_IMO[m.mmsi]||'');
          if(posData && posData.lat != null && posData.lon != null){
            var plat = posData.lat, plon = posData.lon;
            m.curLat = plat; m.curLon = plon;
            process.stderr.write(m.vessel+": current pos lat="+plat.toFixed(2)+" lon="+plon.toFixed(2)+"\n");
            var offRoute = false, reason = "";
            // East Pacific / Americas (western hemisphere away from Europe approach)
            if(plon < -30 && plon > -170){ offRoute = true; reason = "Americas/E.Pacific"; }
            // West/Central Pacific east of Japan (heading away from Singapore)
            else if(plon > 141 && plon < 200){ offRoute = true; reason = "Pacific (east of Japan)"; }
            else if(plon < -170 || plon > 200){ offRoute = true; reason = "Mid-Pacific"; }
            // Far north (Sea of Okhotsk / north Pacific rotations)
            else if(plat > 46 && plon > 135){ offRoute = true; reason = "North Pacific/Okhotsk"; }
            // China coast north of Shanghai heading into Yellow Sea/Bohai (China routes)
            else if(plat > 32 && plon >= 117 && plon <= 127){ offRoute = true; reason = "Yellow Sea/China coast"; }
            if(offRoute){
              process.stderr.write(m.vessel+": OFF-ROUTE position ("+reason+"), hard reject -50\n");
              m.europeScore -= 50;
            }
          }
        } catch(e){ process.stderr.write("Position check failed for "+m.vessel+": "+e.message+"\n"); }

        // TEMPORAL SANITY CHECK — vessel can't be carrying a car loaded AFTER
        // the vessel already arrived at the destination on its current rotation.
        // For NAGOYA leg only: if the ship was already at a European port
        // before the car's leftTheFactory date, the car can't be on this ship
        // (it left Europe before the car was even ready).
        // This check does NOT apply to zeebrugge/malmo feeder legs — for those,
        // being at ZCT before the LeftTheDepot date is exactly expected: the
        // feeder vessel arrived at Zeebrugge, loaded the car, then departed.
        // Applying this check to feeder legs incorrectly hard-rejects every
        // valid candidate (confirmed: Danube Highway and Celeste ACE both
        // correctly rejected as false positives when this ran on zeebrugge leg).
        if(LEG === 'nagoya'){
        try {
          var depMs = new Date(D+"T00:00:00Z").getTime();
          var trackUrl = 'https://shipinfo.net/topos/api/vessel/track?days=60&imo='+
                         safeNum(VESSEL_IMO[m.mmsi]||'')+'&mmsi='+safeNum(m.mmsi);
          var trackResp = await fetch(trackUrl);
          var trackData = await trackResp.json();
          var trackPts = Array.isArray(trackData) ? trackData : (trackData.data || trackData.points || []);
          // EU port boxes (rough — covers any northern European arrival)
          var EU_BOXES = [
            { name:"ZCT",        latMin:51.29, latMax:51.32, lonMin:3.21, lonMax:3.24 },
            { name:"Bremerhaven",latMin:53.55, latMax:53.62, lonMin:8.55, lonMax:8.62 },
            { name:"Southampton",latMin:50.88, latMax:50.92, lonMin:-1.45,lonMax:-1.38 },
            { name:"Sagunto",    latMin:39.62, latMax:39.66, lonMin:-0.25,lonMax:-0.20 },
          ];
          var arrivedBefore = null;
          for(var ti=0; ti<trackPts.length; ti++){
            var pt = trackPts[ti];
            if(!pt.lat || !pt.lng) continue;
            var pms = new Date(pt.updated).getTime();
            if(pms >= depMs) continue;  // only points BEFORE the depart date
            for(var bi=0; bi<EU_BOXES.length; bi++){
              var b = EU_BOXES[bi];
              if(pt.lat>=b.latMin && pt.lat<=b.latMax &&
                 pt.lng>=b.lonMin && pt.lng<=b.lonMax &&
                 (pt.speed_kn||0)<=1){
                arrivedBefore = { port: b.name, when: pt.updated };
                break;
              }
            }
            if(arrivedBefore) break;
          }
          if(arrivedBefore){
            var daysBefore = Math.round((depMs - new Date(arrivedBefore.when).getTime())/86400000);
            process.stderr.write(m.vessel+": at "+arrivedBefore.port+" "+daysBefore+
              " days BEFORE depart date — car not on this ship, hard reject -50\n");
            m.europeScore -= 50;
          }
        } catch(e){ process.stderr.write("Temporal check failed for "+m.vessel+": "+e.message+"\n"); }
        } // end nagoya-only block

        process.stderr.write(m.vessel+": europeScore="+m.europeScore+"\n");
      }catch(e){ m.europeScore=0; }
    }
    matches.sort(function(a,b){return (b.europeScore||0)-(a.europeScore||0);});
    // NAGOYA only: reject when the best match has no European ports at all —
    // the deep-sea leg is picking a Japan-departing ship, so no European port
    // history is a strong "not this one" signal. On FEEDER legs every candidate
    // is already European by construction, and other signals (the -5 for
    // "destination isn't the next hub" I added earlier) can push europeScore
    // to zero or negative on real candidates. Applying the reject there dropped
    // every ship before berth confirmation and Last Trips could weigh in —
    // which is why detection returned nothing despite six berth-confirmed
    // candidates being found.
    if(LEG === 'nagoya' && (matches[0].europeScore||0) === 0){
      process.stderr.write("Best match "+matches[0].vessel+" europeScore=0, rejecting — not Europe route\n");
      matches=[];
    }
    // For nagoya, zeebrugge and malmo legs: verify vessel was at correct berth
    if((LEG === 'nagoya' || LEG === 'zeebrugge' || LEG === 'malmo') && matches.length > 0){
      for(var vi=0; vi<matches.length; vi++){
        var vm = matches[vi];
        if(!vm.mmsi) continue;
        // Anchor the berth check on THIS vessel's own departure time from the
        // port listing, not on D.
        //
        // D is when the CAR reached the hub; each candidate sailed on its own
        // date, potentially days later. verifyBerth looks for stationary
        // positions in the [anchor-6d, anchor] window, so passing D asked
        // "was this ship at the Zeebrugge berth in the six days before the car
        // arrived" — for Elbe Highway that meant 07-14..07-20 when it was
        // actually berthed on 07-24/25 before its 07-25 14:15 departure.
        // Every candidate scored 0 hits and was rejected, including the right
        // one. The listing row already carries the exact departure time.
        var berthAnchor = (vm.time && /^\d{4}-\d{2}-\d{2}/.test(vm.time))
                          ? vm.time.slice(0,10) : D;
        if(berthAnchor !== D){
          process.stderr.write(vm.vessel+': berth check anchored on its own departure '+
                               berthAnchor+' (not D='+D+')\n');
        }
        var berthOk = await verifyBerth(vm.mmsi, vm.imo||VESSEL_IMO[vm.mmsi]||'', berthAnchor, LEG);
        if(berthOk === 'E5'){
          // Confirmed at Toyota Europe berth — strong positive signal
          process.stderr.write(vm.vessel+': CONFIRMED at E5 (Europe berth) ✅\n');
          vm.europeScore += 15;
          vm.berthConfirmed = true;
        } else if(berthOk === false){
          // Not found at ANY known Nagoya Toyota berth — unknown vessel position
          process.stderr.write(vm.vessel+': NOT at any known Nagoya Toyota berth\n');
          vm.europeScore = -1;
        } else if(typeof berthOk === 'string' && berthOk !== 'E5'){
          // Named berth that is mixed-use (W5, KINJO — europe:null) or
          // confirmed non-Europe (europe:false).
          // Look up the berth definition to decide impact.
          var bDef = NAGOYA_BERTHS.find(function(b){ return b.name === berthOk; });
          if(bDef && bDef.europe === false){
            // Confirmed non-Europe only berth — eliminate
            process.stderr.write(vm.vessel+': at '+berthOk+' (non-Europe berth), removing\n');
            vm.europeScore = -1;
          } else {
            // Mixed-use berth (europe:null) — confirmed at Nagoya Toyota
            // terminal but berth alone can't prove Europe routing.
            // Treat as neutral: keep candidate, let other signals decide.
            // Also mark berthConfirmed so the "require E5" gate doesn't
            // eliminate it — we've confirmed the ship was physically at
            // a Toyota Nagoya berth, which is meaningful even if not E5.
            process.stderr.write(vm.vessel+': at '+berthOk+' (mixed-use berth) — keeping, let route scoring decide\n');
            vm.europeScore += 5;   // small positive: confirmed at Nagoya Toyota terminal
            vm.berthConfirmed = true;
          }
        } else if(berthOk === true){
          // Zeebrugge/Malmo confirmed
          process.stderr.write(vm.vessel+': CONFIRMED at '+LEG+' berth ✅\n');
          vm.europeScore += 10;
          vm.berthConfirmed = true;
        } else if(berthOk === null){
          // AIS check could not complete (shipinfo had no data, network error etc.).
          // For nagoya leg, treat this as STRONG NEGATIVE — we can't confirm the ship
          // was at E5, and unverified detection has produced wrong results before.
          // For other legs (zeebrugge/malmo), keep neutral as before.
          if(LEG === 'nagoya'){
            process.stderr.write(vm.vessel+': berth check returned no data, treating as unconfirmed -10\n');
            vm.europeScore -= 10;
          }
        }
      }
      // ── Feeder tie-break: pick the berth-confirmed ship that actually
      // ── carried the car ─────────────────────────────────────────────────
      // Every berth-confirmed candidate spent time at the Zeebrugge terminal
      // recently, so they all look the same to europeScore. The distinguishing
      // fact is when EACH SHIP sailed vs when the car arrived at Malmo — the
      // one whose departure sits within (WINDOW_END - transit .. WINDOW_END)
      // is the one that actually did the run. This is data we already have,
      // not another heuristic.
      if(IS_FEEDER_LEG && WINDOW_END && /^\d{4}-\d{2}-\d{2}$/.test(WINDOW_END)){
        var arrival = new Date(WINDOW_END+"T00:00:00Z").getTime();
        var maxTransitMs = (parseInt(process.env.FEEDER_TRANSIT_DAYS ||
          ({zeebrugge:3,malmo:3,gothenburg:3,drammen:3,southampton:3,portbury:3,
            bremerhaven:3,sagunto:5,livorno:5,piraeus:5}[LEG]||3), 10)) * 86400000;
        for(var ki=0; ki<matches.length; ki++){
          var mm = matches[ki];
          if(!mm.berthConfirmed || !mm.time) continue;
          // Row time from MST is UTC-ish; treat as UTC. Small skew does not
          // matter because we only need the ORDER of departures.
          var t = Date.parse(mm.time.replace(" ", "T")+"Z");
          if(isNaN(t)) continue;
          // In-window: sailed AFTER (arrival - maxTransit) and BEFORE arrival.
          // Both bounds inclusive with 12h slack, since MST times can be slightly off.
          var earliest = arrival - maxTransitMs - 12*3600*1000;
          var latest   = arrival + 12*3600*1000;
          // Ships that departed AFTER the car reached the next hub cannot have
          // carried it there. Small negative window (up to 12h) is tolerated
          // because the two clocks — MST departure time and our observation of
          // the car arriving — can slip a little.
          // The only hard fact we have is: a ship that DEPARTED after the car
          // arrived at the next hub cannot possibly have carried it. Anything
          // else — "how many hours before is best" — is a guess about voyage
          // duration that I have been wrong about repeatedly today. So we do
          // exactly the elimination and no positive scoring: it lets the other
          // signals (europeScore, berth confirmation, page destination) pick
          // the winner among ships that were physically capable of the trip.
          //
          // The 6h slack is calibration: MST departure time and our observation
          // of the car at the next hub come from different clocks, and a ship
          // that departed 6h "after" arrival probably actually sailed just
          // before it — but 12h+ is inarguable.
          if(t > arrival + 6*3600*1000){
            var hoursAfter = Math.round((t - arrival)/3600000);
            process.stderr.write(mm.vessel+': departed '+mm.time+' — '+
              hoursAfter+'h AFTER car reached next hub, cannot be the carrier -40\n');
            mm.europeScore -= 40;
          } else if(t >= earliest && t <= latest){
            var hoursBeforeArrival = Math.round((arrival - t)/3600000);
            process.stderr.write(mm.vessel+': departed '+mm.time+' ('+
              hoursBeforeArrival+'h before car reached next hub) — plausible carrier\n');
            mm.plausibleCarrier = true;
          } else {
            var deltaH = Math.round((t - arrival)/3600000);
            process.stderr.write(mm.vessel+': departed '+mm.time+' (' +
              (deltaH>0?'+':'')+deltaH+'h vs arrival) — outside plausible carrier window\n');
          }
        }
      }

      matches.sort(function(a,b){return (b.europeScore||0)-(a.europeScore||0);});
      matches = matches.filter(function(m){ return (m.europeScore||0) >= 0; });
      // For NAGOYA leg, require berth confirmation. If no candidate was
      // confirmed at E5, return no match rather than guessing — preventing
      // wrong vessels like Elbe Highway (already in Europe) from being
      // assigned just because their europeScore was high. Other legs
      // (zeebrugge/malmo) keep the looser scoring-based winner.
      if(LEG === 'nagoya'){
        var confirmed = matches.filter(function(m){ return m.berthConfirmed === true; });
        if(confirmed.length > 0){
          matches = confirmed;
        } else {
          process.stderr.write('No vessel berth-confirmed at E5 from port scrape, '+
            'trying reverse-lookup against known Toyota carriers...\n');
          matches = [];
        }
      }
    }
  }

  // REVERSE-LOOKUP PASS — when port scrape fails for nagoya, scan all known
  // Toyota Europe carriers' AIS tracks for E5 hits in the departure window.
  // MST's Nagoya port-departure feed doesn't list PCCs reliably; shipinfo's
  // satellite AIS does. This finds the right ship even when port scrape fails.
  if(LEG === 'nagoya' && matches.length === 0){
    process.stderr.write('Reverse-lookup: checking '+Object.keys(TOYOTA_CARRIERS).length+
                         ' known Toyota carriers for E5 visits around '+D+'...\n');
    var depMs = new Date(D+"T00:00:00Z").getTime();
    var winStart = depMs - 6*86400000;   // D-6 (6 days before email)
    var winEnd   = depMs - 1*86400000;   // D-1 (strictly before email arrival)
    // Check ALL known Toyota Nagoya berths (E5 primary, but also W5/KINJO
    // which are confirmed mixed-use — Orchid Leader loaded European cars at
    // KINJO/W5 in May 2026, never touching E5)
    var NAGOYA_CHECK_BERTHS = NAGOYA_BERTHS; // use the shared berth definitions

    var candidates = [];
    var mmsiList = Object.keys(TOYOTA_CARRIERS);
    // Run in parallel batches of 5 to keep total time manageable
    var batchSize = 5;
    for(var bi=0; bi<mmsiList.length; bi+=batchSize){
      var batch = mmsiList.slice(bi, bi+batchSize);
      var results = await Promise.all(batch.map(async function(mmsi){
        try {
          var imo = VESSEL_IMO[mmsi] || '';
          if(!imo) return null;  // shipinfo needs IMO to return data
          var url = 'https://shipinfo.net/topos/api/vessel/track?days=20&imo='+safeNum(imo)+'&mmsi='+safeNum(mmsi);
          var resp = await fetch(url);
          var data = await resp.json();
          var pts = Array.isArray(data) ? data : (data.data || data.points || []);
          // Check ALL Toyota Nagoya berths in the departure window
          var berthHits = [];
          var bestBerth = null;
          NAGOYA_CHECK_BERTHS.forEach(function(b){
            var hits = pts.filter(function(p){
              if(!p.lat || !p.lng) return false;
              var ts = new Date(p.updated).getTime();
              return ts >= winStart && ts <= winEnd &&
                     p.lat >= b.latMin && p.lat <= b.latMax &&
                     p.lng >= b.lonMin && p.lng <= b.lonMax &&
                     (p.speed_kn||0) <= 1;
            });
            if(hits.length > 0 && (!bestBerth || b.europe === true)){
              berthHits = hits;
              bestBerth = b;
            }
          });
          if(berthHits.length > 0){
            // Latest berth hit = the actual departure time
            berthHits.sort(function(a,b){ return new Date(b.updated)-new Date(a.updated); });
            var lastHit = new Date(berthHits[0].updated).getTime();
            // Score: E5 hits score highest, mixed berths lower but still positive
            var berthScore = (bestBerth.europe === true) ? 15 : 5;
            return {
              mmsi: mmsi,
              vessel: TOYOTA_CARRIERS[mmsi],
              berthName: bestBerth.name,
              berthHits: berthHits.length,
              lastBerth: berthHits[0].updated,
              berthConfirmed: true,
              score: berthScore + berthHits.length * 2 -
                     Math.abs(lastHit - depMs)/86400000
            };
          }
          return null;
        } catch(e) {
          process.stderr.write('  '+mmsi+' lookup failed: '+e.message+'\n');
          return null;
        }
      }));
      results.forEach(function(r){ if(r) candidates.push(r); });
    }

    if(candidates.length > 0){
      // Filter out candidates whose CURRENT position proves they are not on
      // the Europe voyage (e.g. heading into Pacific, back to Japan, etc).
      // Same logic as the main scoring's geographic check.
      // ALSO filter by ROUTE REGION — Mediterranean ships serve Med orders,
      // Northern Europe ships serve Northern orders. A ship's 60-day track
      // tells us which region it serves on its current rotation.

      // Classify each candidate's recent destinations into a region.
      async function vesselRegion(mmsi){
        try {
          var imo = VESSEL_IMO[mmsi] || '';
          if(!imo) return null;
          var url = 'https://shipinfo.net/topos/api/vessel/track?days=120&imo='+safeNum(imo)+'&mmsi='+safeNum(mmsi);
          var resp = await fetch(url);
          var data = await resp.json();
          var pts = Array.isArray(data) ? data : (data.data || data.points || []);
          // Look at stationary points in known European port boxes
          var medScore = 0, northScore = 0;
          // Mediterranean port boxes
          var MED = [
            { latMin:39.62, latMax:39.66, lonMin:-0.25, lonMax:-0.20 },  // Sagunto
            { latMin:43.54, latMax:43.58, lonMin:10.28, lonMax:10.33 },  // Livorno
            { latMin:37.92, latMax:37.97, lonMin:23.60, lonMax:23.65 },  // Piraeus
            { latMin:28.13, latMax:28.17, lonMin:-15.45,lonMax:-15.40 }, // Las Palmas
            { latMin:34.65, latMax:34.70, lonMin:33.00, lonMax:33.07 },  // Limassol
            { latMin:36.55, latMax:36.65, lonMin:35.80, lonMax:36.20 },  // Iskenderun
            { latMin:33.85, latMax:33.95, lonMin:35.45, lonMax:35.55 },  // Beirut
          ];
          // Northern Europe port boxes
          var NORTH = [
            { latMin:51.29, latMax:51.33, lonMin:3.18,  lonMax:3.24 },  // Zeebrugge
            { latMin:53.54, latMax:53.62, lonMin:8.55,  lonMax:8.65 },  // Bremerhaven
            { latMin:50.88, latMax:50.92, lonMin:-1.45, lonMax:-1.38 }, // Southampton
            { latMin:55.60, latMax:55.65, lonMin:12.98, lonMax:13.05 }, // Malmö
            { latMin:53.50, latMax:53.55, lonMin:9.85,  lonMax:10.05 }, // Hamburg
            { latMin:51.95, latMax:52.00, lonMin:4.10,  lonMax:4.20 },  // Rotterdam
          ];
          pts.forEach(function(p){
            if(!p.lat || !p.lng || (p.speed_kn||0) > 1) return;
            MED.forEach(function(b){
              if(p.lat>=b.latMin&&p.lat<=b.latMax&&p.lng>=b.lonMin&&p.lng<=b.lonMax) medScore++;
            });
            NORTH.forEach(function(b){
              if(p.lat>=b.latMin&&p.lat<=b.latMax&&p.lng>=b.lonMin&&p.lng<=b.lonMax) northScore++;
            });
          });
          if(medScore > 0 && medScore > northScore*2) return "MEDITERRANEAN";
          if(northScore > 0 && northScore > medScore*2) return "NORTHERN";
          if(medScore === 0 && northScore === 0) return null;
          return "MIXED";  // serves both regions, can't discriminate
        } catch(e){ return null; }
      }

      var goodCandidates = [];
      for(var ci=0; ci<candidates.length; ci++){
        var cnd = candidates[ci];
        try {
          var curPos = await getShipinfoPosition(cnd.mmsi, VESSEL_IMO[cnd.mmsi]||'');
          if(!curPos || curPos.lat == null || curPos.lon == null){
            process.stderr.write('  '+cnd.vessel+': no current pos, skipping\n');
            continue;
          }
          var plat = curPos.lat, plon = curPos.lon;
          var offRoute = false, reason = "";
          if(plon < -30 && plon > -170){ offRoute = true; reason = "Americas"; }
          else if(plon > 141 && plon < 200){ offRoute = true; reason = "Pacific east of Japan"; }
          else if(plon < -170 || plon > 200){ offRoute = true; reason = "Mid-Pacific"; }
          else if(plat > 46 && plon > 135){ offRoute = true; reason = "North Pacific"; }
          else if(plat > 32 && plon >= 117 && plon <= 127){ offRoute = true; reason = "Yellow Sea"; }
          // Japan-port destination filter REMOVED — AIS dest field is often
          // a stale last-port-call value (e.g. Cepheus showed HITACHI from a
          // prior loading stop, but was actually heading SG SIN to Europe).
          // Position-based geographic check (above) is the reliable signal.
          if(offRoute){
            process.stderr.write('  '+cnd.vessel+': REJECTED ('+reason+
              ', pos lat='+plat.toFixed(1)+' lon='+plon.toFixed(1)+
              (curPos.dest?', dest='+curPos.dest:'')+')\n');
            continue;
          }
            // ROUTE REGION CHECK — only if we know the order's destination region
            if(ORDER_REGION){
              var vRegion = await vesselRegion(cnd.mmsi);
              if(vRegion && vRegion !== "MIXED" && vRegion !== ORDER_REGION){
                process.stderr.write('  '+cnd.vessel+': REJECTED (serves '+vRegion+
                  ' route, order is '+ORDER_REGION+')\n');
                continue;
              }
              cnd.vRegion = vRegion;
            }
          cnd.curLat = plat; cnd.curLon = plon; cnd.curDest = curPos.dest;
          goodCandidates.push(cnd);
        } catch(e){
          process.stderr.write('  '+cnd.vessel+': position check failed: '+e.message+'\n');
        }
      }
      candidates = goodCandidates;
    }

    if(candidates.length > 0){
      candidates.sort(function(a,b){ return b.score - a.score; });
      process.stderr.write('Reverse-lookup '+candidates.length+' candidate(s) after route filter:\n');
      candidates.forEach(function(c){
        process.stderr.write('  '+c.vessel+' ('+c.mmsi+'): '+c.berthHits+
                             ' hits at '+c.berthName+', last '+c.lastBerth+', score '+c.score.toFixed(1)+
                             ', pos '+(c.curLat?c.curLat.toFixed(1)+','+c.curLon.toFixed(1):'?')+
                             (c.curDest?', dest='+c.curDest:'')+'\n');
      });
      var winner = candidates[0];
      process.stderr.write('Winner: '+winner.vessel+' at '+winner.berthName+' berth (reverse-lookup)\n');
      matches = [{
        mmsi: winner.mmsi,
        vessel: winner.vessel,
        time: winner.lastBerth,
        europeScore: winner.berthName === 'E5' ? 100 : 80,  // E5=high confidence, mixed berth=good confidence
        berthConfirmed: true
      }];
    } else {
      process.stderr.write('Reverse-lookup: no known Toyota carrier was at any Toyota Nagoya berth in window '+
                           new Date(winStart).toISOString().slice(0,10)+' to '+
                           new Date(winEnd).toISOString().slice(0,10)+
                           ' AND heading on Europe route\n');
    }
  }

  result.total=rows.length;
  result.matches=matches;
  result.leg=LEG;
  if(matches.length>0){
    result.mmsi=matches[0].mmsi;
    // Pass berth_verified flag through so web.py can lock the detection
    result.berth_verified = matches[0].berthConfirmed === true;

    // Close runners-up. On a feeder leg it is genuinely common for two ships
    // (e.g. Elbe Highway and Seine Highway on Zeebrugge->Malmo) to both do the
    // trip in the same window, and Toyota can book on either — the display
    // should say "Elbe Highway or Seine Highway" instead of pretending we know.
    // Threshold: within 40 points on feeder legs (the +30 destination bonus and
    // +100 Last Trips bonus create large gaps between genuine alternates on the
    // same route), within 10 points on deep-sea legs where the field is wider.
    var top = matches[0].europeScore || 0;
    var altThreshold = IS_FEEDER_LEG ? 40 : 10;
    result.alternates = matches.slice(1)
      .filter(function(m){ return (m.europeScore||0) >= top - altThreshold && m.mmsi; })
      .slice(0, 3)
      .map(function(m){ return {mmsi: m.mmsi, name: m.vessel, score: m.europeScore||0}; });
    if(result.alternates.length){
      process.stderr.write("Close runners-up: "+
        result.alternates.map(function(a){return a.name+" ("+a.score+")";}).join(", ")+"\n");
    }
  }
}

// Scrape MST vessel DETAIL page for destination + ETA.
// The map feed (vesselsonmaptempTTT) has NO destination; the detail page does.
// Returns {dest, eta} or null. Uses the in-browser page context to avoid blocks.
async function getMstDetail(pg, mmsi, imo, name){
  try {
    // Build the detail-page slug: name-mmsi-MMSI-imo-IMO
    var slug = safeSlug(name||TOYOTA_CARRIERS[mmsi]);
    var url = "https://www.myshiptracking.com/vessels/"+slug+"-mmsi-"+safeNum(mmsi)+(imo?("-imo-"+safeNum(imo)):"");
    var html = await pg.evaluate(async function(u){
      try { var r = await fetch(u); return await r.text(); } catch(e){ return ""; }
    }, url);
    if(!html) return null;
    // Destination: the page has TWO "myst-arrival-cont" blocks —
    //   1st = last port (e.g. KOBE, wrapped in <a>)
    //   2nd = actual destination (e.g. "SG SIN PEBGA", plain text)
    // Collect all port h3 blocks, strip nested tags, take the LAST one.
    var dest = null;
    var h3re = /<h3 class="text-truncate m-1">([\s\S]*?)<\/h3>/g;
    var ports = [], hm;
    while((hm = h3re.exec(html)) !== null){
      var clean = hm[1].replace(/<[^>]+>/g, "").trim();  // strip <a> etc.
      if(clean) ports.push(clean);
    }
    if(ports.length > 0){
      // Last port block = destination (first is the departed-from port)
      dest = ports[ports.length - 1];
    }
    // ETA: after "ETA*" label, the date span (+ time)
    var eta = null;
    var etaIdx = html.indexOf("ETA*");
    if(etaIdx >= 0){
      var eseg = html.slice(etaIdx, etaIdx+220);
      var ed = eseg.match(/<span class="line">([\d]{4}-[\d]{2}-[\d]{2})<\/span>\s*<span class="line"><b>([^<]+)<\/b>/);
      if(ed) eta = ed[1] + " " + ed[2].trim();
      else {
        var ed2 = eseg.match(/<span class="line">([\d]{4}-[\d]{2}-[\d]{2})<\/span>/);
        if(ed2) eta = ed2[1];
      }
    }
    if(dest || eta){
      process.stderr.write("MST detail: dest="+dest+" eta="+eta+"\n");
      return { dest: dest, eta: eta };
    }
    return null;
  } catch(e) {
    process.stderr.write("MST detail scrape failed: "+e.message+"\n");
    return null;
  }
}

// Get position
if(result.mmsi){
  var apiUrl="https://www.myshiptracking.com/requests/vesselonmap.php?type=json&mmsi="+safeNum(result.mmsi)+"&_="+Date.now();
  var apiResp=await pg.evaluate(async function(url){var r=await fetch(url);return await r.text();},apiUrl);
  process.stderr.write("MST API: "+apiResp.slice(0,80)+"\n");
  var parts=apiResp.trim().split(/\s+/);
  var lat=parts[0]?parseFloat(parts[0]):null;
  var lon=parts[1]?parseFloat(parts[1]):null;
  var speed=parts[2]?parseFloat(parts[2]):null;
  var ageMin=parts[3]?parseInt(parts[3]):99999;

  // Destination + ETA come from the MST detail page (map feed has neither)
  var detail = await getMstDetail(pg, result.mmsi, VESSEL_IMO[result.mmsi]||'', TOYOTA_CARRIERS[result.mmsi]);
  var dest = detail ? detail.dest : null;
  var eta  = detail ? detail.eta  : null;

  result.position={
    lat:lat, lon:lon, speed:speed, dest:dest, eta:eta,
    name:TOYOTA_CARRIERS[result.mmsi]||"",
    source:"myshiptracking", ageMin:ageMin
  };

  if(ageMin > 60){
    process.stderr.write("MST stale ("+ageMin+"min), trying shipinfo.net...\n");
    // Try shipinfo.net first — best satellite AIS coverage for deep-sea ships
    var siPos=await getShipinfoPosition(result.mmsi, VESSEL_IMO[result.mmsi]||'');
    if(siPos && siPos.lat && siPos.ageMin < ageMin){
      process.stderr.write("shipinfo fresher ("+siPos.ageMin+"min vs "+ageMin+"min)\n");
      result.position=Object.assign({}, result.position, {
        lat:siPos.lat, lon:siPos.lon, speed:siPos.speed,
        course:(siPos.course != null ? siPos.course : result.position.course),
        dest:(result.position.dest || siPos.dest || null),
        eta:(result.position.eta || siPos.eta || null),
        ageMin:siPos.ageMin, source:"shipinfo"
      });
      ageMin = siPos.ageMin;
    }
    // If still stale, try ShipFinder as last resort
    if(ageMin > 60){
      process.stderr.write("Still stale, trying ShipFinder...\n");
      var sfPos=await getShipFinderPosition(result.mmsi);
      if(sfPos&&sfPos.lat){
        // Take position from ShipFinder but PRESERVE the MST destination
        // (dest is stable; ShipFinder often has none). Same for name/course.
        result.position=Object.assign({}, result.position, {
          lat:sfPos.lat, lon:sfPos.lon, speed:sfPos.speed,
          course:(sfPos.course != null ? sfPos.course : result.position.course),
          dest:(result.position.dest || sfPos.dest || null),
          eta:(result.position.eta || null),
          ageMin:sfPos.ageMin != null ? sfPos.ageMin : result.position.ageMin,
          source:"shipfinder",
          name:result.position.name
        });
      }
    }
  }
}

process.stdout.write(JSON.stringify(result)+"\n");
}catch(err){
process.stderr.write("ERR:"+err.message+"\n");
process.stdout.write(JSON.stringify({error:err.message})+"\n");
}finally{await br.close();}
})();