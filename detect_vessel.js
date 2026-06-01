const{chromium}=require("playwright");
const E=process.env.MST_EMAIL||"";
const P=process.env.MST_PASSWORD||"";
const D=process.argv[2];   // leftTheFactory date OR visited date for intermediate legs
const MMSI=process.argv[3]||"";
const LEG=process.argv[4]||"nagoya"; // which leg to detect: nagoya|zeebrugge|malmo

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

// Toyota E5 berth coordinates at Nagoya port
var E5_LAT_MIN = 35.04, E5_LAT_MAX = 35.06;
var E5_LON_MIN = 136.87, E5_LON_MAX = 136.90;

// Zeebrugge Car Terminal (ZCT) coordinates
// Confirmed from Wild Rose Leader, Elbe Highway, Garnet Leader 2 AIS data
var ZCT_LAT_MIN = 51.295, ZCT_LAT_MAX = 51.315;
var ZCT_LON_MIN = 3.215,  ZCT_LON_MAX = 3.240;

// Malmö car terminal (Skandiahamnen) coordinates
// Confirmed from Elbe Highway, Danube Highway AIS data
var MALMO_LAT_MIN = 55.610, MALMO_LAT_MAX = 55.630;
var MALMO_LON_MIN = 12.990, MALMO_LON_MAX = 13.015;

async function verifyBerth(mmsi, imo, departDate, leg) {
  try {
    var latMin, latMax, lonMin, lonMax;
    if(leg === 'nagoya'){
      latMin=E5_LAT_MIN; latMax=E5_LAT_MAX; lonMin=E5_LON_MIN; lonMax=E5_LON_MAX;
    } else if(leg === 'zeebrugge'){
      latMin=ZCT_LAT_MIN; latMax=ZCT_LAT_MAX; lonMin=ZCT_LON_MIN; lonMax=ZCT_LON_MAX;
    } else if(leg === 'malmo'){
      latMin=MALMO_LAT_MIN; latMax=MALMO_LAT_MAX; lonMin=MALMO_LON_MIN; lonMax=MALMO_LON_MAX;
    } else {
      return null; // no berth verification for other legs
    }
    var url = 'https://shipinfo.net/topos/api/vessel/track?days=60&imo='+imo+'&mmsi='+mmsi;
    var resp = await fetch(url);
    var data = await resp.json();
    var points = Array.isArray(data) ? data : (data.data || data.points || []);
    var lf = new Date(departDate+'T00:00:00Z');
    var window_start = new Date(lf.getTime() - 7*86400000);
    var hits = points.filter(function(p){
      if(!p.lat || !p.lng) return false;
      var t = new Date(p.updated);
      return t >= window_start && t <= lf &&
             latMin <= p.lat && p.lat <= latMax &&
             lonMin <= p.lng && p.lng <= lonMax &&
             (p.speed_kn||0) <= 1;
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
            course: crsM ? parseFloat(crsM[1]) : 0,
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

  // Departure window: 5 days before the date (compensates for late logins)
  var lf=new Date(D+"T00:00:00Z");
  var start=Math.floor((lf.getTime()-5*86400000)/1000);
  var end=Math.floor(lf.getTime()/1000);
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
        var berthOk = await verifyBerth(vm.mmsi, '', D, LEG);
        if(berthOk === false){
          process.stderr.write(vm.vessel+': NOT at '+LEG+' berth, removing\n');
          vm.europeScore = -1;
        } else if(berthOk === true){
          process.stderr.write(vm.vessel+': CONFIRMED at '+LEG+' berth ✅\n');
          vm.europeScore += 10;
        }
      }
      matches.sort(function(a,b){return (b.europeScore||0)-(a.europeScore||0);});
      matches = matches.filter(function(m){ return (m.europeScore||0) >= 0; });
    }
  }

  result.total=rows.length;
  result.matches=matches;
  result.leg=LEG;
  if(matches.length>0) result.mmsi=matches[0].mmsi;
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

  var destResp=await pg.evaluate(async function(mmsi){
    var url="https://www.myshiptracking.com/requests/vesselsonmaptempTTT.php?type=json&minlat=-90&maxlat=90&minlon=-180&maxlon=180&zoom=2&selid="+mmsi+"&seltype=0&timecode=-1&filters=%7B%7D";
    var r=await fetch(url);return await r.text();
  },result.mmsi);
  var destMatch=destResp.match(result.mmsi+"\t([^\t]+)\t[\\d\\.]+\t[\\d\\.]+\t[\\d\\.]+\t[\\d\\.]+\t[\\d]+\t[\\d]+\t[\\d]+\t[\\d]+\t\t[\\d]+\t([A-Z>][^\n\t]*)");
  var name=destMatch?destMatch[1].trim():null;
  var dest=destMatch?destMatch[2].trim().replace(/^>/,""):null;

  result.position={
    lat:lat, lon:lon, speed:speed, dest:dest,
    name:name||TOYOTA_CARRIERS[result.mmsi]||"",
    source:"myshiptracking", ageMin:ageMin
  };

  if(ageMin > 60){
    process.stderr.write("MST stale ("+ageMin+"min), trying ShipFinder...\n");
    var sfPos=await getShipFinderPosition(result.mmsi);
    if(sfPos&&sfPos.lat){
      result.position=Object.assign({},result.position,sfPos,{name:result.position.name});
    }
  }
}

process.stdout.write(JSON.stringify(result)+"\n");
}catch(err){
process.stderr.write("ERR:"+err.message+"\n");
process.stdout.write(JSON.stringify({error:err.message})+"\n");
}finally{await br.close();}
})();