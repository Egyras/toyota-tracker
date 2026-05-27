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
  var matches=rows.filter(function(r){return C.some(function(c){return r.vessel.toUpperCase().indexOf(c)>=0;});});

  // If multiple matches, score by European port history
  if(matches.length > 1){
    process.stderr.write("Multiple matches ("+matches.length+"), scoring...\n");
    var EUROPE=["ZEEBRUGGE","BREMERHAVEN","SOUTHAMPTON","ANTWERP","ROTTERDAM","MALMO","PALDISKI","PORTBURY","SAGUNTO","GOTHENBURG"];
    for(var mi=0;mi<matches.length;mi++){
      var m=matches[mi];
      if(!m.mmsi) continue;
      try {
        var vurl="https://www.myshiptracking.com/vessels/"+m.vessel.toLowerCase().replace(/\s+/g,"-")+"-mmsi-"+m.mmsi;
        await pg.goto(vurl,{timeout:20000});
        await pg.waitForTimeout(3000);
        var vtext=await pg.textContent("body");
        m.europeScore=EUROPE.filter(function(p){return vtext.toUpperCase().includes(p);}).length;
        process.stderr.write(m.vessel+": europeScore="+m.europeScore+"\n");
      } catch(e) { m.europeScore=0; }
    }
    matches.sort(function(a,b){return (b.europeScore||0)-(a.europeScore||0);});
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