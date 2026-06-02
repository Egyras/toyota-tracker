const{chromium}=require("playwright");
const E=process.env.MST_EMAIL||"";
const P=process.env.MST_PASSWORD||"";
const D=process.argv[2];   // leftTheFactory date OR visited date for intermediate legs
const MMSI=process.argv[3]||"";
const LEG=process.argv[4]||"nagoya"; // which leg to detect: nagoya|zeebrugge|malmo
const DEST_COUNTRY=(process.argv[5]||"").toUpperCase();  // order destination country for route matching

// Region classification for reverse-lookup route matching
function destRegion(country){
  if(!country) return null;
  if(/^(LITHUANIA|LATVIA|ESTONIA|FINLAND|SWEDEN|NORWAY|DENMARK|POLAND|GERMANY|BELGIUM|NETHERLANDS|UNITED KINGDOM|IRELAND|UK)$/i.test(country)) return "NORTHERN";
  if(/^(FRANCE|ITALY|SPAIN|GREECE|PORTUGAL|CYPRUS|CROATIA|SLOVENIA|MALTA|TURKEY|LEBANON|ISRAEL)$/i.test(country)) return "MEDITERRANEAN";
  return null;  // unknown — don't filter
}
var ORDER_REGION = destRegion(DEST_COUNTRY);

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
  "477816600":"Danube Highway",
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
  "432716000":"9409340",  // Bishu Highway  ← YOUR SHIP confirmed E5 May 27
  "431323000":"9409352",  // Cepheus Leader
  "432988000":"9342882",  // Libra Leader
  "431946000":"9342918",  // Leo Leader
  "477816600":"9388595",  // Danube Highway
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
var NAGOYA_BERTHS = [
  // E5 — Toyota EUROPE loading berth (confirmed: Bishu Highway, Equuleus Leader, Undine Highway etc.)
  { name:'E5', europe:true,
    latMin:35.048, latMax:35.062, lonMin:136.875, lonMax:136.892 },
  // W5 — Non-Europe routes (confirmed: Dionysos Leader → Americas)
  { name:'W5', europe:false,
    latMin:35.048, latMax:35.062, lonMin:136.848, lonMax:136.862 },
  // Kinjo South — different terminal entirely (Orchid Leader → China)
  { name:'KINJO', europe:false,
    latMin:35.025, latMax:35.040, lonMin:136.788, lonMax:136.808 },
];

async function verifyBerth(mmsi, imo, departDate, leg) {
  try {
    var url = 'https://shipinfo.net/topos/api/vessel/track?days=60&imo='+imo+'&mmsi='+mmsi;
    var resp = await fetch(url);
    var data = await resp.json();
    var points = Array.isArray(data) ? data : (data.data || data.points || []);
    var lf = new Date(departDate+'T00:00:00Z');
    var window_start = new Date(lf.getTime() - 7*86400000);
    // Only stationary points within the time window
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
const br=await chromium.launch({headless:true});
const pg=await (await br.newContext()).newPage();
try{
// Login
await pg.goto("https://www.myshiptracking.com/",{timeout:30000});
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
  var pid = PORT_IDS[LEG] || PORT_IDS["nagoya"];
  var carriers = LEG === "nagoya" ? CARRIERS_LEG1 :
                 LEG === "zeebrugge" || LEG === "malmo" ? CARRIERS_LEG2 : CARRIERS_LEG1;

  // Departure window logic (per forum research):
  // - Ship leaves Nagoya E5 berth 1-2 days BEFORE leftTheFactory notification
  // - But DB records leftTheFactory when user first logs in, which may be days AFTER the notification
  // So real departure = D_date - login_gap - 2.
  // We search: 2 days AFTER D (catches early logins) back to 7 days BEFORE D (catches late logins).
  // Total window: D-7 to D+2 days, centred just before the recorded date.
  var lf=new Date(D+"T00:00:00Z");
  var start=Math.floor((lf.getTime()-7*86400000)/1000);
  var end=Math.floor((lf.getTime()+2*86400000)/1000);
  var u="https://www.myshiptracking.com/ports-arrivals-departures/?mmsi=&pid="+pid+"&type=2&time="+start+"_"+end+"&pp=200";
  process.stderr.write("Port "+LEG+" (pid:"+pid+") URL: "+u+"\n");
  await pg.goto(u,{timeout:30000});
  await pg.waitForTimeout(6000);

  var rows=await pg.evaluate(function(){
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
  });

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
        var vurl="https://www.myshiptracking.com/vessels/"+
                 m.vessel.toLowerCase().replace(/\s+/g,"-")+"-mmsi-"+m.mmsi;
        await pg.goto(vurl,{timeout:20000});
        await pg.waitForTimeout(3000);
        var vtext=await pg.textContent("body");
        m.europeScore=EUROPE.filter(function(p){return vtext.toUpperCase().includes(p);}).length;
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
          'YOKOHAMA','TOKYO','OSAKA','KOBE',  // returning to Japan (westbound vessels)
          'BUSAN','GUANGZHOU','TIANJIN','SHANGHAI','HONG KONG','KAOHSIUNG',
        ];
        // Singapore, Port Klang, Colombo, Suez ARE on the Europe route — never penalise these.
        try {
          var liveApi="https://www.myshiptracking.com/requests/vesselsonmaptempTTT.php?type=json&minlat=-90&maxlat=90&minlon=-180&maxlon=180&zoom=2&selid="+m.mmsi+"&seltype=0&timecode=-1&filters=%7B%7D";
          var liveResp=await pg.evaluate(async function(url){var r=await fetch(url);return await r.text();},liveApi);
          var liveDestMatch=liveResp.match(m.mmsi+"\t[^\t]+\t[\d\.]+\t[\d\.]+\t[\d\.]+\t[\d\.]+\t[\d]+\t[\d]+\t[\d]+\t[\d]+\t\t[\d]+\t([A-Z>][^\n\t]*)");
          if(liveDestMatch){
            var liveDest=liveDestMatch[1].trim().replace(/^>/,"").toUpperCase();
            process.stderr.write(m.vessel+": live dest="+liveDest+"\n");
            var isOffRoute=OFF_ROUTE_DEST.some(function(d){return liveDest.indexOf(d)>=0;});
            if(isOffRoute){
              process.stderr.write(m.vessel+": off-route destination ("+liveDest+"), penalizing -20\n");
              m.europeScore -= 20;
            }
            // Bonus: if already heading to a known Europe port, it's definitely the right ship
            var EUROPE_DEST=['ZEEBRUGGE','BREMERHAVEN','SOUTHAMPTON','PORTBURY',
                             'SAGUNTO','LIVORNO','MALMO','GOTHENBURG','PIRAEUS','DRAMMEN',
                             'ANTWERP','ROTTERDAM','SUEZ','PORT SAID'];
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
        // If the ship was at ZCT (Zeebrugge) or another EU port within ~30 days
        // BEFORE the order's leftTheFactory date, the car can't be on this ship.
        try {
          var depMs = new Date(D+"T00:00:00Z").getTime();
          var trackUrl = 'https://shipinfo.net/topos/api/vessel/track?days=60&imo='+
                         (VESSEL_IMO[m.mmsi]||'')+'&mmsi='+m.mmsi;
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

        process.stderr.write(m.vessel+": europeScore="+m.europeScore+"\n");
      }catch(e){ m.europeScore=0; }
    }
    matches.sort(function(a,b){return (b.europeScore||0)-(a.europeScore||0);});
    // Reject all if best match has no European ports — it's not a Europe route vessel
    if((matches[0].europeScore||0) === 0){
      process.stderr.write("Best match "+matches[0].vessel+" europeScore=0, rejecting — not Europe route\n");
      matches=[];
    }
    // For nagoya, zeebrugge and malmo legs: verify vessel was at correct berth
    if((LEG === 'nagoya' || LEG === 'zeebrugge' || LEG === 'malmo') && matches.length > 0){
      for(var vi=0; vi<matches.length; vi++){
        var vm = matches[vi];
        if(!vm.mmsi) continue;
        var berthOk = await verifyBerth(vm.mmsi, vm.imo||VESSEL_IMO[vm.mmsi]||'', D, LEG);
        if(berthOk === 'E5'){
          // Confirmed at Toyota Europe berth — strong positive signal
          process.stderr.write(vm.vessel+': CONFIRMED at E5 (Europe berth) ✅\n');
          vm.europeScore += 15;
          vm.berthConfirmed = true;
        } else if(berthOk === false){
          // At Nagoya but wrong berth OR not at Nagoya at all
          process.stderr.write(vm.vessel+': NOT at any Nagoya Toyota berth, removing\n');
          vm.europeScore = -1;
        } else if(typeof berthOk === 'string' && berthOk !== 'E5'){
          // Named wrong berth (W5, KINJO) — confirmed non-Europe
          process.stderr.write(vm.vessel+': at '+berthOk+' (non-Europe berth), removing\n');
          vm.europeScore = -1;
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
    var winStart = depMs - 7*86400000;   // D-7
    var winEnd   = depMs + 2*86400000;   // D+2
    // E5 berth box (same as NAGOYA_BERTHS[0])
    var E5_LAT = [35.048, 35.062], E5_LON = [136.875, 136.892];

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
          var url = 'https://shipinfo.net/topos/api/vessel/track?days=20&imo='+imo+'&mmsi='+mmsi;
          var resp = await fetch(url);
          var data = await resp.json();
          var pts = Array.isArray(data) ? data : (data.data || data.points || []);
          // Find E5 stationary points within the departure window
          var e5Hits = pts.filter(function(p){
            if(!p.lat || !p.lng) return false;
            var ts = new Date(p.updated).getTime();
            return ts >= winStart && ts <= winEnd &&
                   p.lat >= E5_LAT[0] && p.lat <= E5_LAT[1] &&
                   p.lng >= E5_LON[0] && p.lng <= E5_LON[1] &&
                   (p.speed_kn||0) <= 1;
          });
          if(e5Hits.length > 0){
            // Latest E5 hit = the actual departure time
            e5Hits.sort(function(a,b){ return new Date(b.updated)-new Date(a.updated); });
            var lastE5 = new Date(e5Hits[0].updated).getTime();
            return {
              mmsi: mmsi,
              vessel: TOYOTA_CARRIERS[mmsi],
              e5Hits: e5Hits.length,
              lastE5: e5Hits[0].updated,
              // Score: more E5 hits + closer to depart date = better match
              score: e5Hits.length * 10 -
                     Math.abs(lastE5 - depMs)/86400000
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
          var url = 'https://shipinfo.net/topos/api/vessel/track?days=120&imo='+imo+'&mmsi='+mmsi;
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
        process.stderr.write('  '+c.vessel+' ('+c.mmsi+'): '+c.e5Hits+
                             ' E5 hits, last '+c.lastE5+', score '+c.score.toFixed(1)+
                             ', pos '+(c.curLat?c.curLat.toFixed(1)+','+c.curLon.toFixed(1):'?')+
                             (c.curDest?', dest='+c.curDest:'')+'\n');
      });
      var winner = candidates[0];
      process.stderr.write('Winner: '+winner.vessel+' (berth-verified via reverse-lookup)\n');
      matches = [{
        mmsi: winner.mmsi,
        vessel: winner.vessel,
        time: winner.lastE5,
        europeScore: 100,  // high confidence: berth-confirmed
        berthConfirmed: true
      }];
    } else {
      process.stderr.write('Reverse-lookup: no known Toyota carrier was at E5 in window '+
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
  }
}

// Fetch freshest position from shipinfo.net satellite AIS track
// (often fresher than MST/ShipFinder for deep-sea vessels)
async function getShipinfoPosition(mmsi, imo){
  try {
    var url = 'https://shipinfo.net/topos/api/vessel/track?days=3&imo='+(imo||'')+'&mmsi='+mmsi;
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

// Scrape MST vessel DETAIL page for destination + ETA.
// The map feed (vesselsonmaptempTTT) has NO destination; the detail page does.
// Returns {dest, eta} or null. Uses the in-browser page context to avoid blocks.
async function getMstDetail(pg, mmsi, imo, name){
  try {
    // Build the detail-page slug: name-mmsi-MMSI-imo-IMO
    var slug = (name||TOYOTA_CARRIERS[mmsi]||"vessel")
                 .toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"");
    var url = "https://www.myshiptracking.com/vessels/"+slug+"-mmsi-"+mmsi+(imo?("-imo-"+imo):"");
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
  var apiUrl="https://www.myshiptracking.com/requests/vesselonmap.php?type=json&mmsi="+result.mmsi+"&_="+Date.now();
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