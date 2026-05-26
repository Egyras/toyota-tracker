const{chromium}=require("playwright");
const E=process.env.MST_EMAIL||"";
const P=process.env.MST_PASSWORD||"";
const D=process.argv[2];
const MMSI=process.argv[3]||"";

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
  "477307600":"Morning Highway"
}; // optional: pass MMSI directly to get position only

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
await pg.evaluate(function(){ open_login_window(); });
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

// If MMSI provided directly, skip detection and just get position
if(MMSI){
  result.mmsi=MMSI;
  result.matches=[{mmsi:MMSI,vessel:"",time:""}];
} else {
  // Detect vessel from Nagoya departures
  const lf=new Date(D+"T00:00:00Z");
  const start=Math.floor((lf.getTime()-2*86400000)/1000);
  const end=Math.floor((lf.getTime()-1*86400000)/1000);
  const u="https://www.myshiptracking.com/ports-arrivals-departures/?mmsi=&pid=4715&type=2&time="+start+"_"+end+"&pp=100";
  process.stderr.write("Departures URL: "+u+"\n");
  await pg.goto(u,{timeout:30000});
  await pg.waitForTimeout(6000);
  const rows=await pg.evaluate(function(){
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
  var C=["HIGHWAY","LEADER","ACE","TOREADOR","MORNING"];
  var matches=rows.filter(function(r){return C.some(function(c){return r.vessel.toUpperCase().indexOf(c)>=0;});});
  result.total=rows.length;
  result.matches=matches;
  if(matches.length>0) result.mmsi=matches[0].mmsi;
}

// Fetch position from MarineTraffic by scraping the vessel page
async function getMTPosition(page, mmsi) {
  try {
    var captured = null;
    // Intercept MT's internal latestPosition / vesselInfo API calls
    await page.route('**/*', async function(route) {
      var url = route.request().url();
      if(url.indexOf('marinetraffic.com') >= 0 &&
         (url.indexOf('latestPosition') >= 0 || url.indexOf('vesselInfo') >= 0 ||
          url.indexOf('get_vessel') >= 0 || url.indexOf('getVesselInfo') >= 0)) {
        var resp = await route.fetch();
        var text = await resp.text();
        try {
          var d = JSON.parse(text);
          var v = (d.data && d.data[0]) || d.data || d;
          var la = parseFloat(v.LAT || v.lat || 0);
          var lo = parseFloat(v.LON || v.lon || 0);
          if(la && lo) {
            captured = {
              lat:   la,
              lon:   lo,
              speed: parseFloat(v.SPEED || v.speed || 0) / 10,
              dest:  v.DESTINATION || v.destination || null,
              name:  v.NAME || v.name || null,
              source:'marinetraffic'
            };
          }
        } catch(e) {}
        await route.fulfill({response: resp});
      } else {
        await route.continue();
      }
    });
    await page.goto('https://www.marinetraffic.com/en/ais/details/ships/mmsi:'+mmsi, {timeout:40000});
    await page.waitForTimeout(10000);
    await page.unroute('**/*');

    // Fallback: parse __NEXT_DATA__ embedded JSON
    if(!captured || !captured.lat) {
      captured = await page.evaluate(function() {
        var el = document.getElementById('__NEXT_DATA__');
        if(!el) return null;
        try {
          var raw = el.textContent;
          var la = raw.match(/"LAT"\s*:\s*"?([\-\d\.]+)"?/);
          var lo = raw.match(/"LON"\s*:\s*"?([\-\d\.]+)"?/);
          var sp = raw.match(/"SPEED"\s*:\s*"?([\d\.]+)"?/);
          var ds = raw.match(/"DESTINATION"\s*:\s*"([^"]+)"/);
          var nm = raw.match(/"SHIPNAME"\s*:\s*"([^"]+)"/);
          if(la && lo) return {
            lat:   parseFloat(la[1]), lon: parseFloat(lo[1]),
            speed: sp ? parseFloat(sp[1])/10 : 0,
            dest:  ds ? ds[1] : null,
            name:  nm ? nm[1] : null,
            source:'marinetraffic'
          };
        } catch(e) {}
        return null;
      });
    }
    if(captured && captured.lat) {
      process.stderr.write("MT Position: "+JSON.stringify(captured)+"\n");
      return captured;
    }
  } catch(e) {
    process.stderr.write("MT error: "+e.message+"\n");
  }
  return null;
}

// Get live position from direct API endpoint (much faster than loading full page)
if(result.mmsi){
  var apiUrl="https://www.myshiptracking.com/requests/vesselonmap.php?type=json&mmsi="+result.mmsi+"&_="+Date.now();
  process.stderr.write("Position API: "+apiUrl+"\n");
  var apiResp=await pg.evaluate(async function(url){
    var r=await fetch(url);
    return await r.text();
  },apiUrl);
  process.stderr.write("API response: "+apiResp.slice(0,100)+"\n");

  // Response is tab-separated: lat\tlon\tspeed\tage_minutes
  var parts=apiResp.trim().split(/\s+/);
  var lat=parts[0]?parseFloat(parts[0]):null;
  var lon=parts[1]?parseFloat(parts[1]):null;
  var speed=parts[2]?parseFloat(parts[2]):null;
  var ageMin=parts[3]?parseInt(parts[3]):0;
  process.stderr.write("MST data age: "+ageMin+" minutes\n");

  // Also get destination from vesselsonmaptemp which has more detail
  var destResp=await pg.evaluate(async function(mmsi){
    var url="https://www.myshiptracking.com/requests/vesselsonmaptempTTT.php?type=json&minlat=-90&maxlat=90&minlon=-180&maxlon=180&zoom=2&selid="+mmsi+"&seltype=0&timecode=-1&filters=%7B%7D";
    var r=await fetch(url);
    return await r.text();
  },result.mmsi);

  // Parse destination from line like: 7\t0\t431262000\tHAMBURG HIGHWAY\t34.91869\t136.72725\t7.1\t1.5\t25\t175\t27\t11\t\t1779311208\tJP YKK
  var destMatch=destResp.match(result.mmsi+"\t([^\t]+)\t[\d\.]+\t[\d\.]+\t[\d\.]+\t[\d\.]+\t[\d]+\t[\d]+\t[\d]+\t[\d]+\t\t[\d]+\t([A-Z>][^\n\t]*)");
  var name=destMatch?destMatch[1].trim():null;
  var dest=destMatch?destMatch[2].trim().replace(/^>/,""):null;

  result.position={
    lat:    lat,
    lon:    lon,
    speed:  speed,
    dest:   dest,
    name:   name||TOYOTA_CARRIERS[result.mmsi]||"",
    source: "myshiptracking",
    ageMin: ageMin
  };
  process.stderr.write("Position: "+JSON.stringify(result.position)+"\n");

  // MST data stale (>60 min) — try MarineTraffic for fresher coordinates
  if(ageMin > 60) {
    process.stderr.write("MST data stale ("+ageMin+" min), trying MarineTraffic...\n");
    var mtPos = await getMTPosition(pg, result.mmsi);
    if(mtPos && mtPos.lat) {
      result.position = Object.assign({}, result.position, mtPos,
        {name: result.position.name || mtPos.name}); // keep known vessel name
    }
  }
}

process.stdout.write(JSON.stringify(result)+"\n");
}catch(err){
process.stderr.write("ERR:"+err.message+"\n");
process.stdout.write(JSON.stringify({error:err.message})+"\n");
}finally{await br.close();}
})();