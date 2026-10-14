/**
 * Builds conference schedules (weeks 7-16) on the Group Stage tab, copying the
 * Big 12's Week 7 box so every box matches the existing conferences.
 *
 * Run: Extensions -> Apps Script -> paste -> pick a function -> Run.
 *   buildPac12, buildBigEast, buildAmerican, buildMountainWest,
 *   buildCUSA, buildSunBelt, buildMAC, buildBigSky,
 *   buildA10, buildBigWest, buildCAA, buildHorizon,
 *   buildSummit, buildMetro, buildSouthland, buildUAC,
 *   buildOhioValley, buildMissouriValley, buildAmericaEast,
 *   buildPatriot, buildWestCoast                    - one conference
 *   buildAll                                   - every conference below
 * Re-running is safe: each run wipes and rebuilds that conference's column.
 */
const TAB = 'Group Stage';     // change if the tab is named differently
const START_ROW = 108;         // where every conference's Week 7 header sits
const FIRST_WEEK = 7;          // Round 1 = Week 7 unless a conference sets firstWeek
const TEMPLATE_COL = 2;        // column B: the Big 12 block
const WIDTH = 45;              // one conference block, e.g. B..AT
const AWAY_OFFSET = 29;        // away team is 29 columns right of home (B -> AE)

const CONFERENCES = {
  pac12: {
    col: 186, // GD
    teams: {
      Boise: 'Boise State Broncos', ColoSt: 'Colorado State Rams',
      Fresno: 'Fresno State Bulldogs', Gonzaga: 'Gonzaga Bulldogs',
      OreSt: 'Oregon State Beavers', SDSU: 'San Diego State Aztecs',
      TexasSt: 'Texas State Bobcats', UtahSt: 'Utah State Aggies', WSU: 'WSU Esports',
    },
    weeks: [
      'Fresno-Boise;Boise-UtahSt;SDSU-TexasSt;WSU-ColoSt;WSU-OreSt;SDSU-Fresno;Gonzaga-OreSt;TexasSt-ColoSt',
      'OreSt-ColoSt;UtahSt-TexasSt;OreSt-UtahSt;Gonzaga-WSU;Gonzaga-Boise;Fresno-WSU;TexasSt-Boise;ColoSt-SDSU',
      'WSU-SDSU;UtahSt-Boise;TexasSt-WSU;OreSt-Boise;Fresno-UtahSt;ColoSt-Gonzaga;OreSt-TexasSt',
      'ColoSt-Boise;SDSU-UtahSt;WSU-Boise;UtahSt-Gonzaga;ColoSt-Fresno;OreSt-SDSU;Fresno-TexasSt',
      'TexasSt-Gonzaga;UtahSt-ColoSt;OreSt-Fresno;ColoSt-OreSt;Boise-SDSU;SDSU-Gonzaga;UtahSt-WSU',
      'Gonzaga-Fresno;UtahSt-OreSt;Boise-TexasSt;UtahSt-SDSU;ColoSt-WSU;OreSt-Gonzaga;Fresno-ColoSt',
      'WSU-TexasSt;Boise-OreSt;Boise-Gonzaga;Gonzaga-ColoSt;TexasSt-SDSU;UtahSt-Fresno;SDSU-WSU',
      'WSU-Fresno;Gonzaga-SDSU;Fresno-OreSt;ColoSt-TexasSt;Gonzaga-TexasSt;SDSU-Boise;WSU-UtahSt',
      'TexasSt-UtahSt;TexasSt-Fresno;Gonzaga-UtahSt;Fresno-Gonzaga;SDSU-OreSt;OreSt-WSU;Boise-ColoSt',
      'ColoSt-UtahSt;TexasSt-OreSt;WSU-Gonzaga;Fresno-SDSU;SDSU-ColoSt;Boise-WSU;Boise-Fresno',
    ],
  },
  bigEast: {
    col: 232, // HX
    teams: {
      Butler: 'Butler Bulldogs', UConn: 'Connecticut Huskies',
      Creighton: 'Creighton Bluejays', Marquette: 'Marquette',
      Providence: 'Providence Friars', StJohns: 'St Johns Red Storm',
      Xavier: 'Xavier Musketeers',
    },
    weeks: [
      'Providence-Butler;Marquette-Butler;StJohns-Marquette;StJohns-Xavier;UConn-Creighton',
      'UConn-Xavier;Providence-StJohns;Xavier-Marquette;Creighton-Providence;Butler-Creighton',
      'UConn-StJohns;Xavier-Creighton;Butler-UConn;Marquette-Providence',
      'Creighton-Marquette;Butler-Xavier;Providence-UConn;Creighton-StJohns',
      'StJohns-Butler;Marquette-UConn;Creighton-Xavier;Xavier-Providence',
      'Marquette-Xavier;StJohns-Creighton;Providence-Marquette;UConn-Butler',
      'Butler-StJohns;Providence-Creighton;Providence-Xavier;UConn-Marquette',
      'Creighton-Butler;Butler-Marquette;Xavier-UConn;StJohns-Providence',
      'Marquette-StJohns;Creighton-UConn;Butler-Providence;Xavier-StJohns',
      'UConn-Providence;Xavier-Butler;StJohns-UConn;Marquette-Creighton',
    ],
  },
  american: {
    col: 278, // JR
    teams: {
      ECU: 'East Carolina Pirates', Memphis: 'Memphis Tigers', Niner: 'Niner Esports',
      UNT: 'North Texas Mean Green', USF: 'South Florida Bulls', Temple: 'Temple Owls',
      Tulsa: 'Tulsa Golden Hurricane', UAB: 'UAB Blazers', UTSA: 'UTSA Roadrunners',
      Wichita: 'Wichita State Shockers',
    },
    weeks: [
      'UTSA-Niner;UAB-Niner;Tulsa-UAB;ECU-Memphis;Memphis-Temple;Temple-UTSA;Wichita-USF;UNT-ECU',
      'UNT-Wichita;Tulsa-UNT;ECU-UTSA;Niner-USF;Temple-ECU;UTSA-UAB;Wichita-Memphis;USF-Tulsa',
      'UAB-UNT;Tulsa-Temple;Memphis-Tulsa;Temple-Wichita;UNT-Niner;Wichita-ECU;UTSA-USF;USF-UAB',
      'Temple-Niner;Wichita-UTSA;UTSA-UNT;Tulsa-Wichita;ECU-Tulsa;UNT-USF;Niner-Memphis;Memphis-UAB',
      'Niner-ECU;USF-Memphis;Tulsa-UTSA;UAB-Temple;ECU-UAB;Temple-USF;Wichita-Niner;Memphis-UNT',
      'Temple-Memphis;UAB-Wichita;UTSA-Memphis;UAB-Tulsa;Niner-UTSA;USF-ECU;UNT-Temple;Niner-Tulsa',
      'ECU-UNT;UTSA-Temple;UAB-UTSA;Tulsa-USF;USF-Wichita;Memphis-ECU;Niner-UAB;Wichita-UNT',
      'USF-Niner;UAB-USF;Niner-UNT;Memphis-Wichita;UTSA-ECU;Wichita-Temple;UNT-Tulsa;ECU-Temple',
      'Tulsa-Memphis;Temple-Tulsa;UNT-UAB;USF-UTSA;Memphis-Niner;USF-UNT;UTSA-Wichita;ECU-Wichita',
      'UAB-Memphis;Wichita-Tulsa;ECU-Niner;UNT-UTSA;Memphis-USF;Tulsa-ECU;Niner-Temple;Temple-UAB',
    ],
  },
  mountainWest: {
    col: 324, // LL
    teams: {
      GCU: 'Grand Canyon Antelopes', Nevada: 'Nevada Wolfpack',
      SJSU: 'San Jose State Spartans', Davis: 'UC Davis Aggies',
      UNLV: 'UNLV Rebels', UNM: 'UNM Esports',
    },
    weeks: [
      'Davis-UNM;SJSU-UNLV;Nevada-GCU',
      'Davis-Nevada;GCU-UNLV;UNM-SJSU',
      'Nevada-UNM;SJSU-GCU;UNLV-Davis',
      'Nevada-SJSU;Davis-GCU;UNM-UNLV',
      'SJSU-Davis;UNLV-Nevada;GCU-UNM',
      'UNLV-SJSU;UNM-Davis;GCU-Nevada',
      'SJSU-UNM;UNLV-GCU;Nevada-Davis',
      'Davis-UNLV;GCU-SJSU;UNM-Nevada',
      'GCU-Davis;SJSU-Nevada;UNLV-UNM',
      'Nevada-UNLV;Davis-SJSU;UNM-GCU',
    ],
  },
  cusa: {
    col: 370, // NF
    teams: {
      FIU: "Florida International Panthers",
      KSU: "Kennesaw State Owls",
      Liberty: "Liberty Flames",
      MTSU: "Middle Tennessee State Blue Raiders",
      MoSt: "Missouri State Bears",
      NMSU: "New Mexico State Aggies",
      SHSU: "Sam Houston State Bearkats",
      WKU: "Western Kentucky Hilltoppers",
    },
    weeks: [
      'NMSU-FIU;Liberty-SHSU;KSU-Liberty;MoSt-MTSU;WKU-SHSU;MoSt-FIU',
      'Liberty-NMSU;MoSt-WKU;FIU-MTSU;MTSU-KSU;FIU-WKU;KSU-SHSU',
      'MTSU-WKU;FIU-KSU;KSU-MoSt;Liberty-MoSt;SHSU-NMSU;WKU-Liberty',
      'NMSU-Liberty;WKU-MoSt;MTSU-FIU;MTSU-NMSU;SHSU-KSU;Liberty-FIU',
      'NMSU-WKU;WKU-KSU;MTSU-Liberty;NMSU-MoSt;MTSU-MoSt;SHSU-FIU',
      'SHSU-MoSt;MoSt-KSU;NMSU-MTSU;SHSU-Liberty;Liberty-KSU;WKU-FIU',
      'FIU-NMSU;FIU-MoSt;NMSU-KSU;SHSU-MTSU;Liberty-WKU',
      'MoSt-NMSU;Liberty-MTSU;FIU-SHSU;KSU-WKU;SHSU-WKU',
      'WKU-NMSU;KSU-MTSU;FIU-Liberty;MoSt-SHSU;KSU-NMSU',
      'WKU-MTSU;NMSU-SHSU;MTSU-SHSU;KSU-FIU;MoSt-Liberty',
    ],
  },
  sunBelt: {
    col: 416, // OZ
    teams: {
      App: "Appalachian State Mountaineers",
      CCU: "Coastal Carolina Chanticleers",
      GaSo: "Georgia Southern Eagles",
      GSU: "Georgia State Esports",
      JMU: "James Madison Dukes",
      ULL: "Louisiana Ragin' Cajuns",
      LaTech: "Louisiana Tech Bulldogs",
      ODU: "Old Dominion Monarchs",
      USA: "South Alabama Jaguars",
    },
    weeks: [
      'GSU-USA;LaTech-ODU;App-JMU;ULL-CCU;ULL-ODU;JMU-CCU;GaSo-GSU;App-GaSo',
      'USA-JMU;ODU-USA;ODU-CCU;LaTech-ULL;CCU-App;JMU-GSU;GaSo-ULL;LaTech-GSU',
      'GaSo-USA;USA-GSU;JMU-ULL;ODU-JMU;CCU-LaTech;ULL-App;ODU-GSU',
      'GaSo-JMU;ULL-GSU;App-USA;CCU-GaSo;ODU-App;CCU-GSU;USA-LaTech',
      'JMU-LaTech;GSU-App;CCU-ODU;USA-CCU;ODU-GaSo;USA-ULL;App-LaTech',
      'GSU-JMU;GaSo-CCU;ODU-LaTech;CCU-ULL;USA-ODU;USA-App;LaTech-GaSo',
      'LaTech-CCU;USA-GaSo;JMU-App;ULL-JMU;GSU-LaTech;GSU-ODU;App-ULL',
      'ULL-USA;LaTech-JMU;ULL-GaSo;LaTech-App;App-GSU;GaSo-ODU;CCU-JMU',
      'JMU-USA;LaTech-USA;GSU-CCU;JMU-GaSo;ODU-ULL;App-ODU;GaSo-LaTech',
      'GSU-ULL;ULL-LaTech;JMU-ODU;App-CCU;CCU-USA;GSU-GaSo;GaSo-App',
    ],
  },
  mac: {
    col: 462, // QT
    teams: {
      Akron: "Akron Zips",
      Ball: "Ball State Cardinals",
      BGSU: "Bowling Green State Falcons",
      Buff: "Buffalo Bulls",
      EMU: "Eastern Michigan Eagles",
      Kent: "Kent State Golden Flashes",
      Miami: "Miami Redhawks",
      Ohio: "Ohio Bobcats",
      Toledo: "Toledo Rockets",
    },
    weeks: [
      'Kent-BGSU;Kent-Akron;BGSU-Ohio;Ball-Ohio;Ball-Toledo;EMU-Toledo;Akron-Miami;Miami-Buff',
      'EMU-Ball;Toledo-Ohio;Buff-BGSU;Ohio-Kent;Toledo-Buff;Akron-Ball;BGSU-Miami;EMU-Miami',
      'Toledo-BGSU;Ohio-EMU;BGSU-Ball;Ball-Kent;Buff-Miami;Toledo-Miami;Akron-Buff',
      'Ohio-Miami;Ball-Miami;Toledo-Kent;Akron-BGSU;Ohio-Akron;Kent-Buff;Buff-EMU',
      'BGSU-EMU;Miami-Kent;Ohio-Toledo;Buff-Ball;Buff-Ohio;Toledo-Akron;Kent-EMU',
      'Buff-Kent;Toledo-EMU;Miami-BGSU;EMU-Akron;Akron-Ohio;Ohio-Ball;Buff-Toledo',
      'Miami-Toledo;Buff-Akron;EMU-Ohio;Ball-BGSU;Miami-EMU;Kent-Ball;BGSU-Kent',
      'EMU-Kent;Akron-Toledo;EMU-BGSU;Ball-Akron;Ohio-BGSU;Kent-Miami;Ball-Buff',
      'EMU-Buff;BGSU-Akron;BGSU-Buff;Miami-Ohio;Toledo-Ball;Akron-EMU;Kent-Toledo',
      'Ohio-Buff;Miami-Akron;Kent-Ohio;Miami-Ball;Akron-Kent;BGSU-Toledo;Ball-EMU',
    ],
  },
  bigSky: {
    col: 508, // SN
    teams: {
      IdSt: "Idaho State Bengals",
      Idaho: "Idaho Vandals",
      MSU: "Montana State Bobcats",
      UNCO: "Northern Colorado Bears",
      SUU: "Southern Utah Thunderbirds",
      UT: "Utah Tech Trailblazers",
      Weber: "Weber State Wildcats",
    },
    weeks: [
      'UNCO-IdSt;SUU-UNCO;MSU-Idaho;Weber-IdSt;SUU-UT',
      'Idaho-Weber;IdSt-Idaho;UT-UNCO;Weber-SUU;MSU-UT',
      'MSU-SUU;UT-Idaho;UNCO-Weber;IdSt-MSU',
      'IdSt-UT;Idaho-UNCO;Idaho-SUU;Weber-MSU',
      'UT-Weber;Idaho-UT;UNCO-MSU;SUU-IdSt',
      'UNCO-UT;Weber-UNCO;MSU-IdSt;SUU-Idaho',
      'Weber-Idaho;IdSt-SUU;MSU-UNCO;Weber-UT',
      'UT-MSU;IdSt-UNCO;SUU-Weber;Idaho-IdSt',
      'Idaho-MSU;UNCO-SUU;IdSt-Weber;UT-SUU',
      'MSU-Weber;UNCO-Idaho;SUU-MSU;UT-IdSt',
    ],
  },
  a10: {
    col: 554, // UH
    teams: {
      Dayton: "Dayton Flyers",
      GW: "George Washington Revolutionaries",
      URI: "Rhode Island Rams",
      SJU: "St Josephs Hawks",
      SLU: "St Louis Billikens",
      VCU: "VCU Rams",
    },
    weeks: [
      'GW-URI;VCU-Dayton;SJU-SLU',
      'SLU-URI;VCU-SJU;Dayton-GW',
      'URI-VCU;SJU-Dayton;GW-SLU',
      'VCU-SLU;Dayton-URI;SJU-GW',
      'SLU-Dayton;GW-VCU;URI-SJU',
      'Dayton-VCU;SLU-SJU;URI-GW',
      'SJU-VCU;GW-Dayton;URI-SLU',
      'Dayton-SJU;SLU-GW;VCU-URI',
      'URI-Dayton;GW-SJU;SLU-VCU',
      'SJU-URI;VCU-GW;Dayton-SLU',
    ],
  },
  bigWest: {
    col: 600, // WB
    teams: {
      CalPoly: "Cal Poly Mustangs",
      CBU: "California Baptist Lancers",
      CSUF: "CSU Fullerton Titans",
      CSUN: "CSU Northridge Matadors",
      LBSU: "Long Beach State Beach",
      Sac: "Sacramento State Hornets",
      UCI: "UC Irvine Anteaters",
      UCR: "UC Riverside Highlanders",
      UCSD: "UCSD Esports",
      UVU: "Utah Valley Wolverines",
    },
    weeks: [
      'UCSD-UCR;CSUN-CSUF;CSUF-Sac;Sac-LBSU;CBU-CalPoly;UVU-UCI;LBSU-UVU;CalPoly-UCI',
      'CSUF-UVU;UVU-CalPoly;CSUN-UCSD;UCSD-Sac;LBSU-CSUF;UCR-CBU;UCI-UCR;CBU-CSUN',
      'CalPoly-CSUN;UCR-CalPoly;Sac-CBU;UVU-UCR;CBU-LBSU;UCSD-CSUF;LBSU-UCSD;CSUN-UCI',
      'CSUF-CBU;LBSU-UCI;Sac-CalPoly;UCI-Sac;UCSD-UVU;CSUN-UCR;UVU-CSUN;CBU-UCSD',
      'CSUF-CalPoly;LBSU-UCR;UCSD-UCI;CBU-UVU;Sac-CSUN;UCR-Sac;UCI-CSUF;CalPoly-LBSU',
      'CalPoly-CBU;LBSU-Sac;UCI-UVU;CalPoly-UCSD;UVU-Sac;UCI-CBU;CSUN-LBSU;UCR-CSUF',
      'UCR-UCSD;UCI-CalPoly;CBU-UCR;CSUF-CSUN;UCSD-CSUN;Sac-CSUF;CalPoly-UVU;UVU-LBSU',
      'UVU-CSUF;CalPoly-UCR;CSUF-LBSU;Sac-UCSD;UCR-UCI;CSUN-CBU;UCI-CSUN;UCSD-LBSU',
      'UCR-CSUN;UCR-UVU;LBSU-CBU;CSUN-CalPoly;CSUF-UCSD;CBU-Sac;UVU-UCSD;Sac-UCI',
      'CSUN-UVU;CSUF-UCI;UCSD-CBU;LBSU-CalPoly;UCI-LBSU;Sac-UCR;CalPoly-Sac;CBU-CSUF',
    ],
  },
  caa: {
    col: 646, // XV
    teams: {
      Drexel: "Drexel Dragons",
      Elon: "Elon Phoenix",
      Hofstra: "Hofstra Pride",
      NEU: "Northeastern Huskies",
      Towson: "Towson Tigers",
      UNCW: "UNC Wilmington Seahawks",
    },
    weeks: [
      'Hofstra-Elon;NEU-Drexel;Towson-UNCW',
      'UNCW-Drexel;Elon-NEU;Hofstra-Towson',
      'NEU-UNCW;Towson-Elon;Drexel-Hofstra',
      'Towson-NEU;Hofstra-UNCW;Elon-Drexel',
      'NEU-Hofstra;Drexel-Towson;UNCW-Elon',
      'Drexel-NEU;Elon-Hofstra;UNCW-Towson',
      'NEU-Elon;Drexel-UNCW;Towson-Hofstra',
      'UNCW-NEU;Elon-Towson;Hofstra-Drexel',
      'Drexel-Elon;UNCW-Hofstra;NEU-Towson',
      'Hofstra-NEU;Towson-Drexel;Elon-UNCW',
    ],
  },
  horizon: {
    col: 692, // ZP
    firstWeek: 9, // conference play starts week 9
    teams: {
      Milw: "Milwaukee Panthers",
      NIU: "Northern Illinois Huskies",
      NKU: "Northern Kentucky Norse",
      PFW: "Purdue Fort Wayne Mastodons",
      RMU: "Robert Morris Colonials",
    },
    weeks: [
      'Milw-RMU;NKU-NIU;NIU-PFW',
      'NKU-Milw;RMU-NIU;NKU-PFW',
      'PFW-NIU;RMU-NKU;Milw-PFW',
      'PFW-NKU;NIU-RMU;Milw-NKU',
      'PFW-RMU;NIU-Milw',
      'RMU-Milw;NIU-NKU',
      'NKU-RMU;PFW-Milw',
      'Milw-NIU;RMU-PFW',
    ],
  },
  summit: {
    col: 738, // ABJ
    firstWeek: 9, // conference play starts week 9
    teams: {
      NDak: "North Dakota Fighting Hawks",
      Omaha: "Omaha Mavericks",
      SDak: "South Dakota Coyotes",
      SDSU: "South Dakota State Jackrabbits",
      StT: "St Thomas Tommies",
    },
    weeks: [
      'SDSU-StT;SDak-Omaha;Omaha-NDak',
      'SDak-SDSU;StT-Omaha;SDak-NDak',
      'NDak-Omaha;StT-SDak;SDSU-NDak',
      'NDak-SDak;Omaha-StT;SDSU-SDak',
      'NDak-StT;Omaha-SDSU',
      'StT-SDSU;Omaha-SDak',
      'SDak-StT;NDak-SDSU',
      'SDSU-Omaha;StT-NDak',
    ],
  },
  metro: {
    col: 784, // ADD
    teams: {
      Can: "Canisius Golden Griffins",
      Iona: "Iona Gaels",
      Marist: "Marist Red Foxes",
      Merr: "Merrimack Warriors",
      MSM: "Mount St Mary's Mountaineers",
      Siena: "Siena Saints",
    },
    weeks: [
      'Merr-Can;Marist-Siena;Iona-MSM',
      'Iona-Merr;Can-Siena;MSM-Marist',
      'Merr-MSM;Marist-Can;Siena-Iona',
      'Iona-Can;MSM-Siena;Merr-Marist',
      'Marist-Iona;Can-MSM;Siena-Merr',
      'Siena-Marist;MSM-Iona;Can-Merr',
      'Siena-Can;Marist-MSM;Merr-Iona',
      'Can-Marist;Iona-Siena;MSM-Merr',
      'Siena-MSM;Marist-Merr;Can-Iona',
      'Merr-Siena;MSM-Can;Iona-Marist',
    ],
  },
  southland: {
    col: 830, // AEX
    teams: {
      HCU: "Houston Christian Huskies",
      Lamar: "Lamar Cardinals",
      UNO: "LSU New Orleans Privateers",
      McN: "McNeese State Cowboys",
      SFA: "SFA Lumberjacks",
      UTRGV: "UTRGV Vaqueros",
    },
    weeks: [
      'UNO-HCU;UTRGV-SFA;Lamar-McN',
      'SFA-Lamar;HCU-McN;UTRGV-UNO',
      'McN-UTRGV;UNO-SFA;Lamar-HCU',
      'SFA-McN;UTRGV-HCU;UNO-Lamar',
      'McN-UNO;Lamar-UTRGV;HCU-SFA',
      'McN-Lamar;HCU-UNO;SFA-UTRGV',
      'Lamar-SFA;McN-HCU;UNO-UTRGV',
      'HCU-Lamar;UTRGV-McN;SFA-UNO',
      'HCU-UTRGV;McN-SFA;Lamar-UNO',
      'UNO-McN;SFA-HCU;UTRGV-Lamar',
    ],
  },
  uac: {
    col: 876, // AGR
    teams: {
      UALR: "Arkansas Little Rock Trojans",
      UCA: "Central Arkansas Bears",
      EKU: "Eastern Kentucky Colonels",
      FGCU: "Florida Gulf Coast Eagles",
      UNA: "North Alabama Lions",
      UNF: "North Florida Ospreys",
      UTA: "UT Arlington Mavericks",
      UWF: "West Florida Argonauts",
    },
    weeks: [
      'UWF-UTA;EKU-UNF;UTA-UNA;FGCU-UNF;EKU-UCA;UALR-UNA',
      'UCA-UWF;UNF-UALR;UWF-UNA;UNF-UCA;UTA-FGCU;EKU-UALR',
      'UWF-EKU;UTA-EKU;UNF-UWF;UALR-UTA;UNA-FGCU;UCA-UALR',
      'UCA-UNF;UNA-UWF;UCA-FGCU;UALR-EKU;FGCU-UTA;UTA-UNF',
      'UCA-UTA;FGCU-EKU;UALR-UWF;UNA-UNF;FGCU-UALR;UCA-EKU',
      'UALR-UNF;FGCU-UCA;EKU-UWF;UTA-UWF;UNA-UTA;UNA-EKU',
      'UNA-UCA;FGCU-UWF;UTA-UALR;UNF-EKU;UNF-FGCU',
      'UNF-UNA;UTA-UCA;EKU-FGCU;UWF-UALR;UNA-UALR',
      'EKU-UNA;UWF-FGCU;UALR-FGCU;UWF-UCA;UNF-UTA',
      'UWF-UNF;EKU-UTA;FGCU-UNA;UALR-UCA;UCA-UNA',
    ],
  },
  ohioValley: {
    col: 922, // AIL
    firstWeek: 9, // conference play starts week 9
    teams: {
      EIU: "Eastern Illinois Panthers",
      Lind: "Lindenwood Lions",
      SEMO: "Southeast Missouri State Redhawks",
      USI: "Southern Indiana Screaming Eagles",
      TTU: "Tennessee Tech Golden Eagles",
    },
    weeks: [
      'TTU-USI;Lind-TTU;SEMO-EIU',
      'Lind-USI;EIU-TTU;Lind-SEMO',
      'EIU-Lind;USI-TTU;SEMO-USI',
      'USI-Lind;SEMO-Lind;TTU-EIU',
      'USI-EIU;TTU-SEMO',
      'EIU-SEMO;TTU-Lind',
      'Lind-EIU;USI-SEMO',
      'SEMO-TTU;EIU-USI',
    ],
  },
  missouriValley: {
    col: 968, // AKF
    firstWeek: 9, // conference play starts week 9
    teams: {
      IndSt: "Indiana State Sycamores",
      Murray: "Murray State Racers",
      UNI: "Northern Iowa Panthers",
      SIU: "Southern Illinois Salukis",
      UIC: "UI Chicago Flames",
    },
    weeks: [
      'UNI-UIC;IndSt-SIU;Murray-IndSt',
      'Murray-UNI;Murray-SIU;UIC-IndSt',
      'UIC-Murray;SIU-IndSt;UNI-SIU',
      'UNI-Murray;IndSt-UIC;SIU-Murray',
      'IndSt-UNI;SIU-UIC',
      'UIC-UNI;IndSt-Murray',
      'Murray-UIC;SIU-UNI',
      'UNI-IndSt;UIC-SIU',
    ],
  },
  americaEast: {
    col: 1014, // ALZ
    teams: {
      Albany: "Albany Great Danes",
      Bing: "Binghamton Bearcats",
      UNH: "New Hampshire Wildcats",
      NJITB: "NJIT B",
      NJIT: "NJIT Highlanders",
      UML: "UMass Lowell River Hawks",
      UMBC: "UMBC Golden Retrievers",
      UVM: "Vermont Catamounts",
    },
    weeks: [
      'UMBC-Bing;Albany-UVM;Bing-UVM;NJIT-UNH;UML-UNH;UML-NJITB',
      'UML-Albany;UNH-NJITB;UNH-Albany;UMBC-UVM;NJITB-UMBC;Bing-NJIT',
      'UMBC-UML;UVM-NJIT;Albany-Bing;Bing-UML;UNH-UMBC;NJITB-Albany',
      'Albany-UML;Bing-UNH;NJIT-Bing;UVM-UMBC;NJITB-NJIT;NJITB-UNH',
      'NJITB-Bing;NJITB-UML;Albany-UMBC;NJIT-Albany;UVM-UNH;NJIT-UML',
      'NJIT-NJITB;Bing-UMBC;UVM-UML;Albany-UNH;UML-UMBC;UVM-Bing',
      'UVM-NJITB;UNH-NJIT;Bing-Albany;UNH-UML;NJIT-UMBC',
      'UMBC-Albany;UVM-Albany;UNH-UVM;Bing-NJITB;UML-NJIT',
      'UNH-Bing;UML-UVM;Albany-NJIT;UMBC-NJIT;UMBC-NJITB',
      'UML-Bing;Albany-NJITB;NJITB-UVM;NJIT-UVM;UMBC-UNH',
    ],
  },
  patriot: {
    col: 1060, // ANT
    firstWeek: 11, // conference play starts week 11
    teams: {
      Army: "Army Black Knights",
      Boston: "Boston Terriers",
      Loyola: "Loyola Greyhounds",
      Navy: "Navy Midshipmen",
    },
    weeks: [
      'Loyola-Navy;Boston-Army',
      'Navy-Boston;Army-Loyola',
      'Loyola-Boston;Navy-Army',
      'Army-Boston;Navy-Loyola',
      'Loyola-Army;Boston-Navy',
      'Army-Navy;Boston-Loyola',
    ],
  },
  westCoast: {
    col: 1106, // APN
    firstWeek: 11, // conference play starts week 11
    teams: {
      Denver: "Denver Pioneers",
      Pacific: "Pacific Tigers",
      SCU: "Santa Clara Broncos",
      Seattle: "Seattle Redhawks",
    },
    weeks: [
      'Denver-SCU;Seattle-Pacific',
      'SCU-Seattle;Pacific-Denver',
      'Denver-Seattle;SCU-Pacific',
      'Pacific-Seattle;SCU-Denver',
      'Denver-Pacific;Seattle-SCU',
      'Pacific-SCU;Seattle-Denver',
    ],
  },
};

function buildPac12()    { build_('pac12'); }
function buildBigEast()  { build_('bigEast'); }
function buildAmerican() { build_('american'); }
function buildMountainWest() { build_('mountainWest'); }
function buildCUSA() { build_('cusa'); }
function buildSunBelt() { build_('sunBelt'); }
function buildMAC() { build_('mac'); }
function buildBigSky() { build_('bigSky'); }
function buildA10() { build_('a10'); }
function buildBigWest() { build_('bigWest'); }
function buildCAA() { build_('caa'); }
function buildHorizon() { build_('horizon'); }
function buildSummit() { build_('summit'); }
function buildMetro() { build_('metro'); }
function buildSouthland() { build_('southland'); }
function buildUAC() { build_('uac'); }
function buildOhioValley() { build_('ohioValley'); }
function buildMissouriValley() { build_('missouriValley'); }
function buildAmericaEast() { build_('americaEast'); }
function buildPatriot() { build_('patriot'); }
function buildWestCoast() { build_('westCoast'); }
function buildAll()      { Object.keys(CONFERENCES).forEach(build_); }

function build_(key) {
  const conf = CONFERENCES[key];
  const first = conf.firstWeek || FIRST_WEEK;
  const sh = SpreadsheetApp.getActive().getSheetByName(TAB);
  if (!sh) throw new Error('No tab named "' + TAB + '"');

  // check every short name before touching the sheet
  conf.weeks.forEach((w, i) => w.split(';').forEach(g => g.split('-').forEach(t => {
    if (!conf.teams[t]) throw new Error(key + ' week ' + (i + first) + ': unknown team ' + t);
  })));

  // wipe this conference's column from row 108 down (old placeholder)
  const area = sh.getRange(START_ROW, conf.col, sh.getMaxRows() - START_ROW + 1, WIDTH);
  area.breakApart();
  area.clear();

  const header = sh.getRange(START_ROW, TEMPLATE_COL, 3, WIDTH);
  const game = sh.getRange(START_ROW + 3, TEMPLATE_COL, 3, WIDTH);

  let row = START_ROW;
  conf.weeks.forEach((w, i) => {
    header.copyTo(sh.getRange(row, conf.col, 3, WIDTH));
    sh.getRange(row, conf.col).setValue('Week ' + (i + first));
    let r = row + 3;
    w.split(';').forEach(g => {
      const [home, away] = g.split('-');
      game.copyTo(sh.getRange(r, conf.col, 3, WIDTH));
      sh.getRange(r, conf.col).setValue(conf.teams[home]);
      sh.getRange(r, conf.col + AWAY_OFFSET).setValue(conf.teams[away]);
      r += 3;
    });
    row = r + 1;   // one spacer row, then the next week's header
  });
  SpreadsheetApp.getActive().toast(key + ' schedule built');
}
