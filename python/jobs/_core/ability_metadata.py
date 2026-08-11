"""Ability metadata (name, icon, oGCD flag) for any FFXIV action ID.

Lookup order:
1. Hardcoded bundled map (fast, no network) — extend as more jobs are added.
2. Persistent disk cache (`~/.fflogs_efficiency_analyzer/ability_metadata.json`).
3. XIVAPI fetch (slow path) — result is persisted on success so subsequent
   runs on the same machine never re-fetch.

`is_ogcd` derives from `ActionCategory.Name == "Ability"` in XIVAPI — GCDs are
Weaponskill/Spell, oGCDs are Ability.

Safe to call from worker threads.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import CONFIG_DIR, ensure_config_dir_migrated
from xivapi import BASE as _XIVAPI_BASE, SESSION as _SESSION

log = logging.getLogger(__name__)

_CACHE_PATH = CONFIG_DIR / "ability_metadata.json"


@dataclass(frozen=True)
class AbilityMeta:
    name: str
    icon: str          # XIVAPI relative path, e.g. /i/003000/003501.png
    is_ogcd: bool


# Hardcoded bundled mappings shipped with the repo. Anything missing falls
# through to disk cache, then XIVAPI.
BUNDLED: dict[int, AbilityMeta] = {
    # Machinist
    2876:  AbilityMeta("Reassemble",         "/i/003000/003022.png", True),
    2878:  AbilityMeta("Wildfire",           "/i/003000/003018.png", True),
    2887:  AbilityMeta("Dismantle",          "/i/003000/003011.png", True),
    7411:  AbilityMeta("Heated Split Shot",  "/i/003000/003031.png", False),
    7412:  AbilityMeta("Heated Slug Shot",   "/i/003000/003032.png", False),
    7413:  AbilityMeta("Heated Clean Shot",  "/i/003000/003033.png", False),
    7414:  AbilityMeta("Barrel Stabilizer",  "/i/003000/003034.png", True),
    7418:  AbilityMeta("Flamethrower",        "/i/003000/003038.png", False),  # channeled GCD; niche downtime-edge use
    16498: AbilityMeta("Drill",              "/i/003000/003043.png", False),
    16500: AbilityMeta("Air Anchor",         "/i/003000/003045.png", False),
    16501: AbilityMeta("Automaton Queen",    "/i/003000/003501.png", True),
    16502: AbilityMeta("Queen Overdrive",    "/i/003000/003502.png", True),
    16889: AbilityMeta("Tactician",          "/i/003000/003040.png", True),
    17209: AbilityMeta("Hypercharge",        "/i/003000/003041.png", True),
    25787: AbilityMeta("Crowned Collider",   "/i/003000/003047.png", True),   # pet finisher
    25788: AbilityMeta("Chain Saw",          "/i/003000/003048.png", False),
    36978: AbilityMeta("Blazing Shot",       "/i/003000/003506.png", False),
    36979: AbilityMeta("Double Check",       "/i/003000/003507.png", True),
    36980: AbilityMeta("Checkmate",          "/i/003000/003508.png", True),
    36981: AbilityMeta("Excavator",          "/i/003000/003500.png", False),
    36982: AbilityMeta("Full Metal Field",   "/i/003000/003049.png", False),
    # Red Mage — Magicked Swordplay combo variants (Manafication's 3 FREE
    # enchanted GCDs). XIVAPI has no action entry for these ids, so they'd
    # otherwise resolve to a blank icon on the timeline even though they're the
    # SAME skill as the base combo. Bundle them to the base combo's name + icon
    # (cross-checked vs the cached base ids 7527/7528/7529).
    45960: AbilityMeta("Enchanted Riposte",      "/i/003000/003225.png", False),
    45961: AbilityMeta("Enchanted Zwerchhau",    "/i/003000/003226.png", False),
    45962: AbilityMeta("Enchanted Redoublement", "/i/003000/003227.png", False),
    # Paladin (first tank) — bundled so names/icons resolve with NO live XIVAPI
    # fetch at analysis time. A fresh PLD run otherwise fetches ~30 ids
    # concurrently (the ref fan-out) and a couple time out -> "action <id>"
    # placeholders until the disk cache warms. Names/icons verified against XIVAPI
    # (mirror jobs/paladin/data.py). is_ogcd from ActionCategory == "Ability".
    9:     AbilityMeta("Fast Blade",         "/i/000000/000158.png", False),
    15:    AbilityMeta("Riot Blade",         "/i/000000/000156.png", False),
    17:    AbilityMeta("Sentinel",           "/i/000000/000151.png", True),
    20:    AbilityMeta("Fight or Flight",    "/i/000000/000166.png", True),
    22:    AbilityMeta("Bulwark",            "/i/000000/000167.png", True),
    23:    AbilityMeta("Circle of Scorn",    "/i/000000/000161.png", True),
    24:    AbilityMeta("Shield Lob",         "/i/000000/000164.png", False),
    27:    AbilityMeta("Cover",              "/i/002000/002501.png", True),
    30:    AbilityMeta("Hallowed Ground",    "/i/002000/002502.png", True),
    3538:  AbilityMeta("Goring Blade",       "/i/002000/002506.png", False),
    3539:  AbilityMeta("Royal Authority",    "/i/002000/002507.png", False),
    3540:  AbilityMeta("Divine Veil",        "/i/002000/002508.png", True),
    3541:  AbilityMeta("Clemency",           "/i/002000/002509.png", False),
    3542:  AbilityMeta("Sheltron",           "/i/002000/002510.png", True),
    7382:  AbilityMeta("Intervention",       "/i/002000/002512.png", True),
    7384:  AbilityMeta("Holy Spirit",        "/i/002000/002514.png", False),
    7385:  AbilityMeta("Passage of Arms",    "/i/002000/002515.png", True),
    16459: AbilityMeta("Confiteor",          "/i/002000/002518.png", False),
    16460: AbilityMeta("Atonement",          "/i/002000/002519.png", False),
    16461: AbilityMeta("Intervene",          "/i/002000/002520.png", True),
    25746: AbilityMeta("Holy Sheltron",      "/i/002000/002950.png", True),
    25747: AbilityMeta("Expiacion",          "/i/002000/002951.png", True),
    25748: AbilityMeta("Blade of Faith",     "/i/002000/002952.png", False),
    25749: AbilityMeta("Blade of Truth",     "/i/002000/002953.png", False),
    25750: AbilityMeta("Blade of Valor",     "/i/002000/002954.png", False),
    36918: AbilityMeta("Supplication",       "/i/002000/002522.png", False),
    36919: AbilityMeta("Sepulchre",          "/i/002000/002523.png", False),
    36920: AbilityMeta("Guardian",           "/i/002000/002524.png", True),
    36921: AbilityMeta("Imperator",          "/i/002000/002955.png", True),
    36922: AbilityMeta("Blade of Honor",     "/i/002000/002956.png", True),
    # Warrior (second tank) — bundled so names/icons resolve with NO live XIVAPI
    # fetch at analysis time (same reasoning as Paladin: a fresh WAR run otherwise
    # fetches ~25 ids concurrently in the ref fan-out and a couple time out ->
    # "action <id>" placeholders). Names/icons verified against XIVAPI (mirror
    # jobs/warrior/data.py). is_ogcd from ActionCategory == "Ability".
    31:    AbilityMeta("Heavy Swing",       "/i/000000/000260.png", False),
    37:    AbilityMeta("Maim",              "/i/000000/000255.png", False),
    40:    AbilityMeta("Thrill of Battle",  "/i/000000/000263.png", True),
    42:    AbilityMeta("Storm's Path",      "/i/000000/000258.png", False),
    43:    AbilityMeta("Holmgang",          "/i/000000/000266.png", True),
    44:    AbilityMeta("Vengeance",         "/i/000000/000267.png", True),
    45:    AbilityMeta("Storm's Eye",       "/i/000000/000264.png", False),
    46:    AbilityMeta("Tomahawk",          "/i/000000/000261.png", False),
    52:    AbilityMeta("Infuriate",         "/i/002000/002555.png", True),
    3549:  AbilityMeta("Fell Cleave",       "/i/002000/002557.png", False),
    3551:  AbilityMeta("Raw Intuition",     "/i/002000/002559.png", True),
    3552:  AbilityMeta("Equilibrium",       "/i/002000/002560.png", True),
    7386:  AbilityMeta("Onslaught",         "/i/002000/002561.png", True),
    7387:  AbilityMeta("Upheaval",          "/i/002000/002562.png", True),
    7388:  AbilityMeta("Shake It Off",      "/i/002000/002563.png", True),
    7389:  AbilityMeta("Inner Release",     "/i/002000/002564.png", True),
    16464: AbilityMeta("Nascent Flash",     "/i/002000/002567.png", True),
    16465: AbilityMeta("Inner Chaos",       "/i/002000/002568.png", False),
    25751: AbilityMeta("Bloodwhetting",     "/i/002000/002569.png", True),
    25753: AbilityMeta("Primal Rend",       "/i/002000/002571.png", False),
    36923: AbilityMeta("Damnation",         "/i/002000/002573.png", True),
    36924: AbilityMeta("Primal Wrath",      "/i/002000/002574.png", True),
    36925: AbilityMeta("Primal Ruination",  "/i/002000/002050.png", False),
    # Shared tank role actions — render on the tank Defensives lane (kept in sync
    # with jobs/_core/role_actions.py::ROLE_ACTION_IDS).
    3:     AbilityMeta("Sprint",             "/i/000000/000405.png", False),
    7531:  AbilityMeta("Rampart",            "/i/000000/000801.png", True),
    7533:  AbilityMeta("Provoke",            "/i/000000/000803.png", True),
    7535:  AbilityMeta("Reprisal",           "/i/000000/000806.png", True),
    7537:  AbilityMeta("Shirk",              "/i/000000/000810.png", True),
    7538:  AbilityMeta("Interject",          "/i/000000/000808.png", True),
    7540:  AbilityMeta("Low Blow",           "/i/000000/000802.png", True),
    7548:  AbilityMeta("Arm's Length",       "/i/000000/000822.png", True),
    # Shared physical-DPS role actions (kept in sync with ROLE_ACTION_IDS).
    7541:  AbilityMeta("Second Wind",        "/i/000000/000821.png", True),
    7542:  AbilityMeta("Bloodbath",          "/i/000000/000823.png", True),
    7546:  AbilityMeta("True North",         "/i/000000/000830.png", True),
    7549:  AbilityMeta("Feint",              "/i/000000/000828.png", True),
    7863:  AbilityMeta("Leg Sweep",          "/i/000000/000824.png", True),
    7551:  AbilityMeta("Head Graze",         "/i/000000/000848.png", True),
    7553:  AbilityMeta("Foot Graze",         "/i/000000/000842.png", True),
    7554:  AbilityMeta("Leg Graze",          "/i/000000/000843.png", True),
    # Samurai (sixth simulator) — bundled so names/icons resolve with NO live
    # XIVAPI dependency. Probed from a top-SAM pull's masterData.abilities.
    7478:  AbilityMeta("Jinpu",                   "/i/003000/003152.png", False),
    7479:  AbilityMeta("Shifu",                   "/i/003000/003156.png", False),
    7480:  AbilityMeta("Yukikaze",                "/i/003000/003166.png", False),
    7481:  AbilityMeta("Gekko",                   "/i/003000/003158.png", False),
    7482:  AbilityMeta("Kasha",                   "/i/003000/003164.png", False),
    7486:  AbilityMeta("Enpi",                    "/i/003000/003155.png", False),
    7487:  AbilityMeta("Midare Setsugekka",       "/i/003000/003162.png", False),
    7489:  AbilityMeta("Higanbana",               "/i/003000/003160.png", False),
    7490:  AbilityMeta("Hissatsu: Shinten",       "/i/003000/003173.png", True),
    7492:  AbilityMeta("Hissatsu: Gyoten",        "/i/003000/003169.png", True),
    7493:  AbilityMeta("Hissatsu: Yaten",         "/i/003000/003170.png", True),
    7497:  AbilityMeta("Meditate",                "/i/003000/003172.png", True),
    7498:  AbilityMeta("Third Eye",               "/i/003000/003153.png", True),
    7499:  AbilityMeta("Meikyo Shisui",           "/i/003000/003167.png", True),
    16481: AbilityMeta("Hissatsu: Senei",         "/i/003000/003178.png", True),
    16482: AbilityMeta("Ikishoten",               "/i/003000/003179.png", True),
    16486: AbilityMeta("Kaeshi: Setsugekka",      "/i/003000/003183.png", False),
    16487: AbilityMeta("Shoha",                   "/i/003000/003184.png", True),
    25781: AbilityMeta("Ogi Namikiri",            "/i/003000/003187.png", False),
    25782: AbilityMeta("Kaeshi: Namikiri",        "/i/003000/003188.png", False),
    36962: AbilityMeta("Tengentsu",               "/i/003000/003190.png", True),
    36963: AbilityMeta("Gyofu",                   "/i/003000/003191.png", False),
    36964: AbilityMeta("Zanshin",                 "/i/003000/003192.png", True),
    36966: AbilityMeta("Tendo Setsugekka",        "/i/003000/003194.png", False),
    36968: AbilityMeta("Tendo Kaeshi Setsugekka", "/i/003000/003196.png", False),
    # White Mage (first healer) — bundled so names/icons resolve with NO live
    # XIVAPI dependency. Probed via scripts/probe_whm_ids.py (33/33 verified).
    120:   AbilityMeta("Cure",                "/i/000000/000405.png", False),
    124:   AbilityMeta("Medica",              "/i/000000/000408.png", False),
    125:   AbilityMeta("Raise",               "/i/000000/000411.png", False),
    131:   AbilityMeta("Cure III",            "/i/000000/000407.png", False),
    133:   AbilityMeta("Medica II",           "/i/000000/000409.png", False),
    135:   AbilityMeta("Cure II",             "/i/000000/000406.png", False),
    136:   AbilityMeta("Presence of Mind",    "/i/002000/002626.png", True),
    137:   AbilityMeta("Regen",               "/i/002000/002628.png", False),
    139:   AbilityMeta("Holy",                "/i/002000/002629.png", False),
    140:   AbilityMeta("Benediction",         "/i/002000/002627.png", True),
    3569:  AbilityMeta("Asylum",              "/i/002000/002632.png", True),
    3570:  AbilityMeta("Tetragrammaton",      "/i/002000/002633.png", True),
    3571:  AbilityMeta("Assize",              "/i/002000/002634.png", True),
    7430:  AbilityMeta("Thin Air",            "/i/002000/002636.png", True),
    7432:  AbilityMeta("Divine Benison",      "/i/002000/002638.png", True),
    7433:  AbilityMeta("Plenary Indulgence",  "/i/002000/002639.png", True),
    16531: AbilityMeta("Afflatus Solace",     "/i/002000/002640.png", False),
    16532: AbilityMeta("Dia",                 "/i/002000/002641.png", False),
    16534: AbilityMeta("Afflatus Rapture",    "/i/002000/002643.png", False),
    16535: AbilityMeta("Afflatus Misery",     "/i/002000/002644.png", False),
    16536: AbilityMeta("Temperance",          "/i/002000/002645.png", True),
    25859: AbilityMeta("Glare III",           "/i/002000/002646.png", False),
    25860: AbilityMeta("Holy III",            "/i/002000/002647.png", False),
    25861: AbilityMeta("Aquaveil",            "/i/002000/002648.png", True),
    25862: AbilityMeta("Liturgy of the Bell", "/i/002000/002649.png", True),
    37008: AbilityMeta("Aetherial Shift",     "/i/002000/002125.png", True),
    37009: AbilityMeta("Glare IV",            "/i/002000/002126.png", False),
    37010: AbilityMeta("Medica III",          "/i/002000/002127.png", False),
    37011: AbilityMeta("Divine Caress",       "/i/002000/002128.png", True),
    # Magic role actions (caster/healer — shared; used by RDM/WHM lanes)
    7559:  AbilityMeta("Surecast",            "/i/000000/000869.png", True),
    7561:  AbilityMeta("Swiftcast",           "/i/000000/000866.png", True),
    7562:  AbilityMeta("Lucid Dreaming",      "/i/000000/000865.png", True),
    # Healer role actions
    7568:  AbilityMeta("Esuna",               "/i/000000/000884.png", False),
    7571:  AbilityMeta("Rescue",              "/i/000000/000890.png", True),
    16560: AbilityMeta("Repose",              "/i/000000/000891.png", False),
    # Astrologian (second healer) — bundled so names/icons resolve with NO live
    # XIVAPI dependency. Probed via scripts/probe_astrologian_ids.py (36/36
    # verified; is_ogcd matches jobs/astrologian/data.py OGCD_IDS).
    3594:  AbilityMeta("Benefic",              "/i/003000/003126.png", False),
    3595:  AbilityMeta("Aspected Benefic",     "/i/003000/003127.png", False),
    3600:  AbilityMeta("Helios",               "/i/003000/003129.png", False),
    3601:  AbilityMeta("Aspected Helios",      "/i/003000/003130.png", False),
    3606:  AbilityMeta("Lightspeed",           "/i/003000/003135.png", True),
    3610:  AbilityMeta("Benefic II",           "/i/003000/003128.png", False),
    3612:  AbilityMeta("Synastry",             "/i/003000/003139.png", True),
    3613:  AbilityMeta("Collective Unconscious", "/i/003000/003140.png", True),
    3614:  AbilityMeta("Essential Dignity",    "/i/003000/003141.png", True),
    7439:  AbilityMeta("Earthly Star",         "/i/003000/003143.png", True),
    7441:  AbilityMeta("Stellar Explosion",    "/i/003000/003145.png", True),
    7444:  AbilityMeta("Lord of Crowns",       "/i/003000/003147.png", True),
    7445:  AbilityMeta("Lady of Crowns",       "/i/003000/003146.png", True),
    8324:  AbilityMeta("Stellar Detonation",   "/i/003000/003144.png", True),
    16552: AbilityMeta("Divination",           "/i/003000/003553.png", True),
    16553: AbilityMeta("Celestial Opposition", "/i/003000/003142.png", True),
    16554: AbilityMeta("Combust III",          "/i/003000/003554.png", False),
    16556: AbilityMeta("Celestial Intersection", "/i/003000/003556.png", True),
    16557: AbilityMeta("Horoscope",            "/i/003000/003550.png", True),
    16558: AbilityMeta("Horoscope",            "/i/003000/003551.png", True),
    16559: AbilityMeta("Neutral Sect",         "/i/003000/003552.png", True),
    25871: AbilityMeta("Fall Malefic",         "/i/003000/003559.png", False),
    25872: AbilityMeta("Gravity II",           "/i/003000/003560.png", False),
    25873: AbilityMeta("Exaltation",           "/i/003000/003561.png", True),
    25874: AbilityMeta("Macrocosmos",          "/i/003000/003562.png", False),
    25875: AbilityMeta("Microcosmos",          "/i/003000/003563.png", True),
    37017: AbilityMeta("Astral Draw",          "/i/003000/003564.png", True),
    37018: AbilityMeta("Umbral Draw",          "/i/003000/003565.png", True),
    37019: AbilityMeta("Play I",               "/i/003000/003116.png", True),
    37020: AbilityMeta("Play II",              "/i/003000/003117.png", True),
    37021: AbilityMeta("Play III",             "/i/003000/003118.png", True),
    37022: AbilityMeta("Minor Arcana",         "/i/003000/003119.png", True),
    37023: AbilityMeta("The Balance",          "/i/003000/003110.png", True),
    37024: AbilityMeta("The Arrow",            "/i/003000/003112.png", True),
    37025: AbilityMeta("The Spire",            "/i/003000/003115.png", True),
    37026: AbilityMeta("The Spear",            "/i/003000/003113.png", True),
    37027: AbilityMeta("The Bole",             "/i/003000/003111.png", True),
    37028: AbilityMeta("The Ewer",             "/i/003000/003114.png", True),
    37029: AbilityMeta("Oracle",               "/i/003000/003566.png", True),
    37030: AbilityMeta("Helios Conjunction",   "/i/003000/003567.png", False),
    37031: AbilityMeta("Sun Sign",             "/i/003000/003109.png", True),
    # Scholar (third healer) — XIVAPI-verified names/icons/category (2026-07-17).
    # is_ogcd matches jobs/scholar/data.py OGCD_IDS + the job's GCD/oGCD truth (the
    # fairy commands are oGCDs; Broil/Biolysis/Art of War/Ruin II/heal-cast GCDs are
    # not). The heal-only fairy stream is never scored (aux=0).
    166:   AbilityMeta("Aetherflow",           "/i/000000/000510.png", True),
    167:   AbilityMeta("Energy Drain",         "/i/000000/000514.png", True),
    185:   AbilityMeta("Adloquium",            "/i/002000/002801.png", False),
    186:   AbilityMeta("Succor",               "/i/002000/002802.png", False),
    188:   AbilityMeta("Sacred Soil",          "/i/002000/002804.png", True),
    189:   AbilityMeta("Lustrate",             "/i/002000/002805.png", True),
    190:   AbilityMeta("Physick",              "/i/000000/000518.png", False),
    3583:  AbilityMeta("Indomitability",       "/i/002000/002806.png", True),
    3585:  AbilityMeta("Deployment Tactics",   "/i/002000/002808.png", True),
    3586:  AbilityMeta("Emergency Tactics",    "/i/002000/002809.png", True),
    3587:  AbilityMeta("Dissipation",          "/i/002000/002810.png", True),
    7423:  AbilityMeta("Aetherpact",           "/i/002000/002687.png", True),
    7434:  AbilityMeta("Excogitation",         "/i/002000/002813.png", True),
    7436:  AbilityMeta("Chain Stratagem",      "/i/002000/002815.png", True),
    16537: AbilityMeta("Whispering Dawn",      "/i/002000/002852.png", True),
    16538: AbilityMeta("Fey Illumination",     "/i/002000/002853.png", True),
    16539: AbilityMeta("Art of War",           "/i/002000/002819.png", False),
    16540: AbilityMeta("Biolysis",             "/i/002000/002820.png", False),
    16542: AbilityMeta("Recitation",           "/i/002000/002822.png", True),
    16543: AbilityMeta("Fey Blessing",         "/i/002000/002854.png", True),
    16545: AbilityMeta("Summon Seraph",        "/i/002000/002850.png", True),
    16546: AbilityMeta("Consolation",          "/i/002000/002851.png", True),
    17215: AbilityMeta("Summon Eos",           "/i/002000/002823.png", False),
    17216: AbilityMeta("Summon Selene",        "/i/000000/000786.png", False),
    17870: AbilityMeta("Ruin II",              "/i/000000/000502.png", False),
    25865: AbilityMeta("Broil IV",             "/i/002000/002875.png", False),
    25866: AbilityMeta("Art of War II",        "/i/002000/002876.png", False),
    25867: AbilityMeta("Protraction",          "/i/002000/002877.png", True),
    25868: AbilityMeta("Expedient",            "/i/002000/002878.png", True),
    37012: AbilityMeta("Baneful Impaction",    "/i/002000/002879.png", True),
    37013: AbilityMeta("Concitation",          "/i/002000/002880.png", False),
    37014: AbilityMeta("Seraphism",            "/i/002000/002881.png", True),
    # Sage (fourth healer, second shield healer) — XIVAPI-verified names/icons/
    # category (scripts/probe_sage_ids.py, 39/39; 2026-07-18). is_ogcd matches
    # jobs/sage/data.py OGCD_IDS + the job's GCD/oGCD truth: Psyche is the lone
    # damage oGCD; Eukrasia + the Eukrasian follow-ups + Pneuma + Toxikon + Dosis/
    # Phlegma/Dyskrasia are GCDs; the Addersgall/utility heals are oGCDs.
    24283: AbilityMeta("Dosis",                "/i/003000/003651.png", False),
    24284: AbilityMeta("Diagnosis",            "/i/003000/003652.png", False),
    24285: AbilityMeta("Kardia",               "/i/003000/003653.png", True),
    24286: AbilityMeta("Prognosis",            "/i/003000/003654.png", False),
    24287: AbilityMeta("Egeiro",               "/i/003000/003655.png", False),
    24289: AbilityMeta("Phlegma",              "/i/003000/003657.png", False),
    24290: AbilityMeta("Eukrasia",             "/i/003000/003658.png", False),
    24291: AbilityMeta("Eukrasian Diagnosis",  "/i/003000/003659.png", False),
    24292: AbilityMeta("Eukrasian Prognosis",  "/i/003000/003660.png", False),
    24293: AbilityMeta("Eukrasian Dosis",      "/i/003000/003661.png", False),
    24294: AbilityMeta("Soteria",              "/i/003000/003662.png", True),
    24295: AbilityMeta("Icarus",               "/i/003000/003663.png", True),
    24296: AbilityMeta("Druochole",            "/i/003000/003664.png", True),
    24297: AbilityMeta("Dyskrasia",            "/i/003000/003665.png", False),
    24298: AbilityMeta("Kerachole",            "/i/003000/003666.png", True),
    24299: AbilityMeta("Ixochole",             "/i/003000/003667.png", True),
    24300: AbilityMeta("Zoe",                  "/i/003000/003668.png", True),
    24301: AbilityMeta("Pepsis",               "/i/003000/003669.png", True),
    24302: AbilityMeta("Physis II",            "/i/003000/003670.png", True),
    24303: AbilityMeta("Taurochole",           "/i/003000/003671.png", True),
    24304: AbilityMeta("Toxikon",              "/i/003000/003672.png", False),
    24305: AbilityMeta("Haima",                "/i/003000/003673.png", True),
    24306: AbilityMeta("Dosis II",             "/i/003000/003674.png", False),
    24307: AbilityMeta("Phlegma II",           "/i/003000/003675.png", False),
    24308: AbilityMeta("Eukrasian Dosis II",   "/i/003000/003676.png", False),
    24309: AbilityMeta("Rhizomata",            "/i/003000/003677.png", True),
    24310: AbilityMeta("Holos",                "/i/003000/003678.png", True),
    24311: AbilityMeta("Panhaima",             "/i/003000/003679.png", True),
    24312: AbilityMeta("Dosis III",            "/i/003000/003680.png", False),
    24313: AbilityMeta("Phlegma III",          "/i/003000/003681.png", False),
    24314: AbilityMeta("Eukrasian Dosis III",  "/i/003000/003682.png", False),
    24315: AbilityMeta("Dyskrasia II",         "/i/003000/003683.png", False),
    24316: AbilityMeta("Toxikon II",           "/i/003000/003684.png", False),
    24317: AbilityMeta("Krasis",               "/i/003000/003685.png", True),
    24318: AbilityMeta("Pneuma",               "/i/003000/003686.png", False),
    37032: AbilityMeta("Eukrasian Dyskrasia",  "/i/003000/003687.png", False),
    37033: AbilityMeta("Psyche",               "/i/003000/003688.png", True),
    37034: AbilityMeta("Eukrasian Prognosis II", "/i/003000/003689.png", False),
    37035: AbilityMeta("Philosophia",          "/i/003000/003690.png", True),
    # Dancer (first physical-ranged proc job) — bundled so names/icons resolve
    # with NO live XIVAPI dependency. Probed via scripts/gen_dnc_synthetic_fixture
    # workflow (28/28 ids verified against XIVAPI).
    15989: AbilityMeta("Cascade",                    "/i/003000/003451.png", False),
    15990: AbilityMeta("Fountain",                   "/i/003000/003452.png", False),
    15991: AbilityMeta("Reverse Cascade",            "/i/003000/003460.png", False),
    15992: AbilityMeta("Fountainfall",               "/i/003000/003464.png", False),
    15997: AbilityMeta("Standard Step",              "/i/003000/003454.png", False),
    15998: AbilityMeta("Technical Step",             "/i/003000/003473.png", False),
    15999: AbilityMeta("Emboite",                    "/i/003000/003455.png", False),
    16000: AbilityMeta("Entrechat",                  "/i/003000/003456.png", False),
    16001: AbilityMeta("Jete",                       "/i/003000/003457.png", False),
    16002: AbilityMeta("Pirouette",                  "/i/003000/003458.png", False),
    16005: AbilityMeta("Saber Dance",                "/i/003000/003476.png", False),
    16006: AbilityMeta("Closed Position",            "/i/003000/003470.png", True),
    16007: AbilityMeta("Fan Dance",                  "/i/003000/003462.png", True),
    16008: AbilityMeta("Fan Dance II",               "/i/003000/003466.png", True),
    16009: AbilityMeta("Fan Dance III",              "/i/003000/003472.png", True),
    16010: AbilityMeta("En Avant",                   "/i/003000/003467.png", True),
    16011: AbilityMeta("Devilment",                  "/i/003000/003471.png", True),
    16012: AbilityMeta("Shield Samba",               "/i/003000/003469.png", True),
    16013: AbilityMeta("Flourish",                   "/i/003000/003475.png", True),
    16014: AbilityMeta("Improvisation",              "/i/003000/003477.png", True),
    16015: AbilityMeta("Curing Waltz",               "/i/003000/003468.png", True),
    16192: AbilityMeta("Double Standard Finish",     "/i/003000/003459.png", False),
    16196: AbilityMeta("Quadruple Technical Finish", "/i/003000/003474.png", False),
    25790: AbilityMeta("Tillana",                    "/i/003000/003480.png", False),
    25791: AbilityMeta("Fan Dance IV",               "/i/003000/003481.png", True),
    25792: AbilityMeta("Starfall Dance",             "/i/003000/003482.png", False),
    36983: AbilityMeta("Last Dance",                 "/i/003000/003483.png", False),
    36984: AbilityMeta("Finishing Move",             "/i/003000/003484.png", False),
    36985: AbilityMeta("Dance of the Dawn",          "/i/003000/003485.png", False),
    25789: AbilityMeta("Improvised Finish",          "/i/003000/003479.png", True),
    # The DT level-100 in-log Technical Finish id (16196 above is the base id).
    # XIVAPI returns a wrong icon for 33218, so reuse 16196's correct icon.
    33218: AbilityMeta("Quadruple Technical Finish", "/i/003000/003474.png", False),
    # Black Mage (second caster, first MP-economy job) — bundled so names/icons
    # resolve with NO live XIVAPI fetch at analysis time (same reasoning as the
    # other jobs). Every id below was XIVAPI-verified: the name matched the
    # expected BLM action and the icon + is_ogcd (ActionCategory == "Ability")
    # were captured directly. Mirror jobs/blackmage/data.py.
    141:   AbilityMeta("Fire",                  "/i/000000/000451.png", False),
    142:   AbilityMeta("Blizzard",              "/i/000000/000454.png", False),
    144:   AbilityMeta("Thunder",               "/i/000000/000457.png", False),
    147:   AbilityMeta("Fire II",               "/i/000000/000452.png", False),
    149:   AbilityMeta("Transpose",             "/i/000000/000466.png", True),
    152:   AbilityMeta("Fire III",              "/i/000000/000453.png", False),
    153:   AbilityMeta("Thunder III",           "/i/000000/000459.png", False),
    154:   AbilityMeta("Blizzard III",          "/i/000000/000456.png", False),
    155:   AbilityMeta("Aetherial Manipulation", "/i/000000/000467.png", True),
    157:   AbilityMeta("Manaward",              "/i/000000/000463.png", True),
    158:   AbilityMeta("Manafont",              "/i/002000/002651.png", True),
    159:   AbilityMeta("Freeze",                "/i/002000/002653.png", False),
    162:   AbilityMeta("Flare",                 "/i/002000/002652.png", False),
    3573:  AbilityMeta("Ley Lines",             "/i/002000/002656.png", True),
    3576:  AbilityMeta("Blizzard IV",           "/i/002000/002659.png", False),
    3577:  AbilityMeta("Fire IV",               "/i/002000/002660.png", False),
    7419:  AbilityMeta("Between the Lines",     "/i/002000/002661.png", True),
    7420:  AbilityMeta("Thunder IV",            "/i/002000/002662.png", False),
    7421:  AbilityMeta("Triplecast",           "/i/002000/002663.png", True),
    7422:  AbilityMeta("Foul",                  "/i/002000/002664.png", False),
    16505: AbilityMeta("Despair",               "/i/002000/002665.png", False),
    16506: AbilityMeta("Umbral Soul",           "/i/002000/002666.png", False),
    16507: AbilityMeta("Xenoglossy",            "/i/002000/002667.png", False),
    25793: AbilityMeta("Blizzard II",           "/i/002000/002668.png", False),
    25794: AbilityMeta("High Fire II",          "/i/002000/002669.png", False),
    25795: AbilityMeta("High Blizzard II",      "/i/002000/002670.png", False),
    25796: AbilityMeta("Amplifier",             "/i/002000/002671.png", True),
    25797: AbilityMeta("Paradox",               "/i/002000/002672.png", False),
    36986: AbilityMeta("High Thunder",          "/i/002000/002673.png", False),
    36987: AbilityMeta("High Thunder II",       "/i/002000/002674.png", False),
    36988: AbilityMeta("Retrace",               "/i/002000/002150.png", True),
    36989: AbilityMeta("Flare Star",            "/i/002000/002151.png", False),
    # Magic role action missing from the set above (used by the caster lanes).
    7560:  AbilityMeta("Addle",                 "/i/000000/000861.png", True),
    # Viper (instant-melee, 10th job) — bundled so names/icons resolve with NO
    # live XIVAPI fetch at analysis time (same reasoning as the other jobs). All
    # 29 ids probed from a live top-VPR pull's masterData.abilities (scripts/
    # probe_viper_ids.py) and XIVAPI-verified; is_ogcd matches data.OGCD_IDS.
    34606: AbilityMeta("Steel Fangs",         "/i/003000/003701.png", False),
    34607: AbilityMeta("Reaving Fangs",       "/i/003000/003702.png", False),
    34608: AbilityMeta("Hunter's Sting",      "/i/003000/003703.png", False),
    34609: AbilityMeta("Swiftskin's Sting",   "/i/003000/003704.png", False),
    34610: AbilityMeta("Flanksting Strike",   "/i/003000/003705.png", False),
    34611: AbilityMeta("Flanksbane Fang",     "/i/003000/003706.png", False),
    34612: AbilityMeta("Hindsting Strike",    "/i/003000/003707.png", False),
    34613: AbilityMeta("Hindsbane Fang",      "/i/003000/003708.png", False),
    34620: AbilityMeta("Vicewinder",          "/i/003000/003715.png", False),
    34621: AbilityMeta("Hunter's Coil",       "/i/003000/003716.png", False),
    34622: AbilityMeta("Swiftskin's Coil",    "/i/003000/003717.png", False),
    34626: AbilityMeta("Reawaken",            "/i/003000/003721.png", False),
    34627: AbilityMeta("First Generation",    "/i/003000/003722.png", False),
    34628: AbilityMeta("Second Generation",   "/i/003000/003723.png", False),
    34629: AbilityMeta("Third Generation",    "/i/003000/003724.png", False),
    34630: AbilityMeta("Fourth Generation",   "/i/003000/003725.png", False),
    34631: AbilityMeta("Ouroboros",           "/i/003000/003726.png", False),
    34633: AbilityMeta("Uncoiled Fury",       "/i/003000/003728.png", False),
    34634: AbilityMeta("Death Rattle",        "/i/003000/003729.png", True),
    34636: AbilityMeta("Twinfang Bite",       "/i/003000/003731.png", True),
    34637: AbilityMeta("Twinblood Bite",      "/i/003000/003732.png", True),
    34640: AbilityMeta("First Legacy",        "/i/003000/003735.png", True),
    34641: AbilityMeta("Second Legacy",       "/i/003000/003736.png", True),
    34642: AbilityMeta("Third Legacy",        "/i/003000/003737.png", True),
    34643: AbilityMeta("Fourth Legacy",       "/i/003000/003738.png", True),
    34644: AbilityMeta("Uncoiled Twinfang",   "/i/003000/003739.png", True),
    34645: AbilityMeta("Uncoiled Twinblood",  "/i/003000/003740.png", True),
    34646: AbilityMeta("Slither",             "/i/003000/003741.png", True),
    34647: AbilityMeta("Serpent's Ire",       "/i/003000/003742.png", True),
    # Dragoon (11th job, first beam-forking melee) — names/icons/oGCD flags probed
    # live from XIVAPI (scripts/probe_dragoon_ids.py) so they resolve with NO live
    # fetch. All action IDs verified against a top M11S pull's masterData.abilities.
    75:    AbilityMeta("True Thrust",         "/i/000000/000310.png", False),
    83:    AbilityMeta("Life Surge",          "/i/000000/000304.png", True),
    85:    AbilityMeta("Lance Charge",        "/i/000000/000309.png", True),
    86:    AbilityMeta("Doom Spike",          "/i/000000/000306.png", False),
    90:    AbilityMeta("Piercing Talon",      "/i/000000/000315.png", False),
    94:    AbilityMeta("Elusive Jump",        "/i/002000/002577.png", True),
    96:    AbilityMeta("Dragonfire Dive",     "/i/002000/002578.png", True),
    3554:  AbilityMeta("Fang and Claw",       "/i/002000/002582.png", False),
    3555:  AbilityMeta("Geirskogul",          "/i/002000/002583.png", True),
    3556:  AbilityMeta("Wheeling Thrust",     "/i/002000/002584.png", False),
    3557:  AbilityMeta("Battle Litany",       "/i/002000/002585.png", True),
    7397:  AbilityMeta("Sonic Thrust",        "/i/002000/002586.png", False),
    7399:  AbilityMeta("Mirage Dive",         "/i/002000/002588.png", True),
    7400:  AbilityMeta("Nastrond",            "/i/002000/002589.png", True),
    16477: AbilityMeta("Coerthan Torment",    "/i/002000/002590.png", False),
    16478: AbilityMeta("High Jump",           "/i/002000/002591.png", True),
    16479: AbilityMeta("Raiden Thrust",       "/i/002000/002592.png", False),
    16480: AbilityMeta("Stardiver",           "/i/002000/002593.png", True),
    25770: AbilityMeta("Draconian Fury",      "/i/002000/002594.png", False),
    25771: AbilityMeta("Heavens' Thrust",     "/i/002000/002595.png", False),
    25772: AbilityMeta("Chaotic Spring",      "/i/002000/002596.png", False),
    25773: AbilityMeta("Wyrmwind Thrust",     "/i/002000/002597.png", True),
    36951: AbilityMeta("Winged Glide",        "/i/002000/002598.png", True),
    36952: AbilityMeta("Drakesbane",          "/i/002000/002599.png", False),
    36953: AbilityMeta("Rise of the Dragon",  "/i/002000/002075.png", True),
    36954: AbilityMeta("Lance Barrage",       "/i/002000/002076.png", False),
    36955: AbilityMeta("Spiral Blow",         "/i/002000/002077.png", False),
    36956: AbilityMeta("Starcross",           "/i/002000/002078.png", True),
    # Ninja (13th job, the first mudra job) — names/icons probed live from XIVAPI
    # (scripts/probe_ninja_ids.py); ids verified against top M11S/M12S-P1 pulls'
    # masterData.abilities. is_ogcd matches data.OGCD_IDS, NOT XIVAPI's category:
    # mudras (0.5s), ninjutsu (1.5s) and the Ten Chi Jin steps (~1.0s) are GCDs on
    # reduced recasts (XIVAPI's "Ability" category would misread them as weaves,
    # breaking the idle/clip pacing that `gcd_recast_mult` scales). Both mudra id
    # families are listed (charged first-of-sequence vs free in-sequence/Kassatsu).
    2240:  AbilityMeta("Spinning Edge",       "/i/000000/000601.png", False),
    2241:  AbilityMeta("Shade Shift",         "/i/000000/000607.png", True),
    2242:  AbilityMeta("Gust Slash",          "/i/000000/000602.png", False),
    2245:  AbilityMeta("Hide",                "/i/000000/000609.png", True),
    2247:  AbilityMeta("Throwing Dagger",     "/i/000000/000614.png", False),
    2254:  AbilityMeta("Death Blossom",       "/i/000000/000615.png", False),
    2255:  AbilityMeta("Aeolian Edge",        "/i/000000/000605.png", False),
    2259:  AbilityMeta("Ten",                 "/i/002000/002901.png", False),
    2261:  AbilityMeta("Chi",                 "/i/002000/002902.png", False),
    2262:  AbilityMeta("Shukuchi",            "/i/002000/002905.png", True),
    2263:  AbilityMeta("Jin",                 "/i/002000/002903.png", False),
    2264:  AbilityMeta("Kassatsu",            "/i/002000/002906.png", True),
    2265:  AbilityMeta("Fuma Shuriken",       "/i/002000/002907.png", False),
    2266:  AbilityMeta("Katon",               "/i/002000/002908.png", False),
    2267:  AbilityMeta("Raiton",              "/i/002000/002912.png", False),
    2268:  AbilityMeta("Hyoton",              "/i/002000/002909.png", False),
    2269:  AbilityMeta("Huton",               "/i/002000/002910.png", False),
    2270:  AbilityMeta("Doton",               "/i/002000/002911.png", False),
    2271:  AbilityMeta("Suiton",              "/i/002000/002913.png", False),
    3563:  AbilityMeta("Armor Crush",         "/i/002000/002915.png", False),
    3566:  AbilityMeta("Dream Within a Dream", "/i/002000/002918.png", True),
    7401:  AbilityMeta("Hellfrog Medium",     "/i/002000/002920.png", True),
    7402:  AbilityMeta("Bhavacakra",          "/i/002000/002921.png", True),
    7403:  AbilityMeta("Ten Chi Jin",         "/i/002000/002922.png", True),
    16488: AbilityMeta("Hakke Mujinsatsu",    "/i/002000/002923.png", False),
    16489: AbilityMeta("Meisui",              "/i/002000/002924.png", True),
    16491: AbilityMeta("Goka Mekkyaku",       "/i/002000/002925.png", False),
    16492: AbilityMeta("Hyosho Ranryu",       "/i/002000/002926.png", False),
    16493: AbilityMeta("Bunshin",             "/i/002000/002927.png", True),
    18805: AbilityMeta("Ten",                 "/i/002000/002901.png", False),
    18806: AbilityMeta("Chi",                 "/i/002000/002902.png", False),
    18807: AbilityMeta("Jin",                 "/i/002000/002903.png", False),
    18873: AbilityMeta("Fuma Shuriken",       "/i/002000/002907.png", False),
    18877: AbilityMeta("Raiton",              "/i/002000/002912.png", False),
    18881: AbilityMeta("Suiton",              "/i/002000/002913.png", False),
    25774: AbilityMeta("Phantom Kamaitachi",  "/i/002000/002929.png", False),
    25775: AbilityMeta("Phantom Kamaitachi",  "/i/002000/002929.png", False),
    25776: AbilityMeta("Hollow Nozuchi",      "/i/002000/002930.png", True),
    25777: AbilityMeta("Forked Raiju",        "/i/002000/002931.png", False),
    25778: AbilityMeta("Fleeting Raiju",      "/i/002000/002932.png", False),
    36957: AbilityMeta("Dokumori",            "/i/000000/000619.png", True),
    36958: AbilityMeta("Kunai's Bane",        "/i/000000/000620.png", True),
    36959: AbilityMeta("Deathfrog Medium",    "/i/002000/002934.png", True),
    36960: AbilityMeta("Zesho Meppo",         "/i/002000/002933.png", True),
    36961: AbilityMeta("Tenri Jindo",         "/i/002000/002935.png", True),
    # Monk (14th job, the first form-cycle job) — names/icons probed live from
    # XIVAPI; ids verified against top M11S/M12S-P1 pulls' masterData.abilities
    # (scripts/probe_monk_ids.py). is_ogcd matches data.OGCD_IDS, NOT XIVAPI's
    # category: Forbidden Meditation is typed "Ability" but triggers the GCD (a
    # 1s GCD-linked recast — the weave misread would break the idle/clip pacing
    # that `gcd_recast_mult` scales).
    61:    AbilityMeta("Twin Snakes",         "/i/000000/000213.png", False),
    65:    AbilityMeta("Mantra",              "/i/000000/000216.png", True),
    66:    AbilityMeta("Demolish",            "/i/000000/000204.png", False),
    69:    AbilityMeta("Perfect Balance",     "/i/000000/000217.png", True),
    70:    AbilityMeta("Rockbreaker",         "/i/002000/002529.png", False),
    74:    AbilityMeta("Dragon Kick",         "/i/002000/002528.png", False),
    3547:  AbilityMeta("the Forbidden Chakra", "/i/002000/002535.png", True),
    4262:  AbilityMeta("Form Shift",          "/i/002000/002536.png", False),
    7394:  AbilityMeta("Riddle of Earth",     "/i/002000/002537.png", True),
    7395:  AbilityMeta("Riddle of Fire",      "/i/002000/002541.png", True),
    7396:  AbilityMeta("Brotherhood",         "/i/002000/002542.png", True),
    16473: AbilityMeta("Four-point Fury",     "/i/002000/002544.png", False),
    16474: AbilityMeta("Enlightenment",       "/i/002000/002545.png", True),
    16476: AbilityMeta("Six-sided Star",      "/i/002000/002547.png", False),
    25762: AbilityMeta("Thunderclap",         "/i/002000/002975.png", True),
    25765: AbilityMeta("Celestial Revolution", "/i/002000/002977.png", False),
    25766: AbilityMeta("Riddle of Wind",      "/i/002000/002978.png", True),
    25767: AbilityMeta("Shadow of the Destroyer", "/i/002000/002979.png", False),
    25768: AbilityMeta("Rising Phoenix",      "/i/002000/002980.png", False),
    25769: AbilityMeta("Phantom Rush",        "/i/002000/002981.png", False),
    36942: AbilityMeta("Forbidden Meditation", "/i/000000/000218.png", False),
    36944: AbilityMeta("Earth's Reply",       "/i/002000/002549.png", True),
    36945: AbilityMeta("Leaping Opo",         "/i/002000/002982.png", False),
    36946: AbilityMeta("Rising Raptor",       "/i/002000/002983.png", False),
    36947: AbilityMeta("Pouncing Coeurl",     "/i/002000/002984.png", False),
    36948: AbilityMeta("Elixir Burst",        "/i/002000/002985.png", False),
    36949: AbilityMeta("Wind's Reply",        "/i/002000/002986.png", False),
    36950: AbilityMeta("Fire's Reply",        "/i/002000/002987.png", False),
    # Bard (15th job, the first song-cycle job) — names/icons probed live from
    # XIVAPI; ids verified against top M11S pulls' masterData.abilities
    # (scripts/probe_bard_ids.py). is_ogcd matches data.OGCD_IDS (XIVAPI's
    # ActionCategory agrees on every id — Pitch Perfect's 1s recast is a true
    # oGCD, unlike NIN's reduced-recast GCD families).
    101:   AbilityMeta("Raging Strikes",      "/i/000000/000352.png", True),
    107:   AbilityMeta("Barrage",             "/i/000000/000353.png", True),
    112:   AbilityMeta("Repelling Shot",      "/i/000000/000366.png", True),
    114:   AbilityMeta("Mage's Ballad",       "/i/002000/002602.png", True),
    116:   AbilityMeta("Army's Paeon",        "/i/002000/002603.png", True),
    117:   AbilityMeta("Rain of Death",       "/i/002000/002605.png", True),
    118:   AbilityMeta("Battle Voice",        "/i/002000/002601.png", True),
    3558:  AbilityMeta("Empyreal Arrow",      "/i/002000/002606.png", True),
    3559:  AbilityMeta("the Wanderer's Minuet", "/i/002000/002607.png", True),
    3560:  AbilityMeta("Iron Jaws",           "/i/002000/002608.png", False),
    3561:  AbilityMeta("the Warden's Paean",  "/i/002000/002609.png", True),
    3562:  AbilityMeta("Sidewinder",          "/i/002000/002610.png", True),
    7404:  AbilityMeta("Pitch Perfect",       "/i/002000/002611.png", True),
    7405:  AbilityMeta("Troubadour",          "/i/002000/002612.png", True),
    7406:  AbilityMeta("Caustic Bite",        "/i/002000/002613.png", False),
    7407:  AbilityMeta("Stormbite",           "/i/002000/002614.png", False),
    7408:  AbilityMeta("Nature's Minne",      "/i/002000/002615.png", True),
    7409:  AbilityMeta("Refulgent Arrow",     "/i/002000/002616.png", False),
    16494: AbilityMeta("Shadowbite",          "/i/002000/002617.png", False),
    16495: AbilityMeta("Burst Shot",          "/i/002000/002618.png", False),
    16496: AbilityMeta("Apex Arrow",          "/i/002000/002619.png", False),
    25783: AbilityMeta("Ladonsbite",          "/i/002000/002620.png", False),
    25784: AbilityMeta("Blast Arrow",         "/i/002000/002621.png", False),
    25785: AbilityMeta("Radiant Finale",      "/i/002000/002622.png", True),
    36974: AbilityMeta("Wide Volley",         "/i/000000/000357.png", False),
    36975: AbilityMeta("Heartbreak Shot",     "/i/002000/002623.png", True),
    36976: AbilityMeta("Resonant Arrow",      "/i/002000/002624.png", False),
    36977: AbilityMeta("Radiant Encore",      "/i/002000/002100.png", False),
    # Gunbreaker (12th job, 3rd tank) — bundled so names/icons resolve with NO
    # live XIVAPI fetch at analysis time (same reasoning as the other jobs), and
    # so the Clipping aspect + the GCD-speed inference / demonstrated-cadence
    # anchor work under the hermetic test stub (they skip any cast whose
    # metadata is None). All ids fixture-verified against real top-GNB pulls'
    # cast streams (tests/fixtures/gnb/) and XIVAPI-verified; is_ogcd matches
    # data.OGCD_IDS (XIVAPI's ActionCategory agrees on every id).
    16137: AbilityMeta("Keen Edge",           "/i/003000/003401.png", False),
    16138: AbilityMeta("No Mercy",            "/i/003000/003402.png", True),
    16139: AbilityMeta("Brutal Shell",        "/i/003000/003403.png", False),
    16140: AbilityMeta("Camouflage",          "/i/003000/003404.png", True),
    16141: AbilityMeta("Demon Slice",         "/i/003000/003405.png", False),
    16142: AbilityMeta("Royal Guard",         "/i/003000/003406.png", True),
    16143: AbilityMeta("Lightning Shot",      "/i/003000/003407.png", False),
    16144: AbilityMeta("Danger Zone",         "/i/003000/003408.png", True),
    16145: AbilityMeta("Solid Barrel",        "/i/003000/003409.png", False),
    16146: AbilityMeta("Gnashing Fang",       "/i/003000/003410.png", False),
    16147: AbilityMeta("Savage Claw",         "/i/003000/003411.png", False),
    16148: AbilityMeta("Nebula",              "/i/003000/003412.png", True),
    16149: AbilityMeta("Demon Slaughter",     "/i/003000/003413.png", False),
    16150: AbilityMeta("Wicked Talon",        "/i/003000/003414.png", False),
    16151: AbilityMeta("Aurora",              "/i/003000/003415.png", True),
    16152: AbilityMeta("Superbolide",         "/i/003000/003416.png", True),
    16153: AbilityMeta("Sonic Break",         "/i/003000/003417.png", False),
    16155: AbilityMeta("Continuation",        "/i/003000/003419.png", True),
    16156: AbilityMeta("Jugular Rip",         "/i/003000/003420.png", True),
    16157: AbilityMeta("Abdomen Tear",        "/i/003000/003421.png", True),
    16158: AbilityMeta("Eye Gouge",           "/i/003000/003422.png", True),
    16159: AbilityMeta("Bow Shock",           "/i/003000/003423.png", True),
    16160: AbilityMeta("Heart of Light",      "/i/003000/003424.png", True),
    16161: AbilityMeta("Heart of Stone",      "/i/003000/003425.png", True),
    16162: AbilityMeta("Burst Strike",        "/i/003000/003426.png", False),
    16163: AbilityMeta("Fated Circle",        "/i/003000/003427.png", False),
    16164: AbilityMeta("Bloodfest",           "/i/003000/003428.png", True),
    16165: AbilityMeta("Blasting Zone",       "/i/003000/003429.png", True),
    25758: AbilityMeta("Heart of Corundum",   "/i/003000/003430.png", True),
    25759: AbilityMeta("Hypervelocity",       "/i/003000/003431.png", True),
    25760: AbilityMeta("Double Down",         "/i/003000/003432.png", False),
    32068: AbilityMeta("Release Royal Guard", "/i/003000/003433.png", True),
    36934: AbilityMeta("Trajectory",          "/i/003000/003434.png", True),
    36935: AbilityMeta("Great Nebula",        "/i/003000/003435.png", True),
    36936: AbilityMeta("Fated Brand",         "/i/003000/003436.png", True),
    36937: AbilityMeta("Reign of Beasts",     "/i/003000/003437.png", False),
    36938: AbilityMeta("Noble Blood",         "/i/003000/003438.png", False),
    36939: AbilityMeta("Lion Heart",          "/i/003000/003439.png", False),

    # Pictomancer (16th job, the first downtime-painting caster) — names/icons
    # probed live from XIVAPI 2026-07-03; every id below appears in real top-pull
    # cast streams (scripts/probe_pictomancer_ids.py). is_ogcd follows the job's
    # truth (jobs/pictomancer/data.py OGCD_IDS): motifs, hammers, Star Prism,
    # Holy/Comet and Rainbow Drip are genuinely GCDs; the Star Prism follow-up
    # (34682, the 0-damage auto second part) is flagged True — it occupies no
    # GCD slot, so the pacing detectors must not count it as one.
    34650: AbilityMeta("Fire in Red",         "/i/003000/003801.png", False),
    34651: AbilityMeta("Aero in Green",       "/i/003000/003802.png", False),
    34652: AbilityMeta("Water in Blue",       "/i/003000/003803.png", False),
    34653: AbilityMeta("Blizzard in Cyan",    "/i/003000/003804.png", False),
    34654: AbilityMeta("Stone in Yellow",     "/i/003000/003805.png", False),
    34655: AbilityMeta("Thunder in Magenta",  "/i/003000/003806.png", False),
    34656: AbilityMeta("Fire II in Red",      "/i/003000/003807.png", False),
    34657: AbilityMeta("Aero II in Green",    "/i/003000/003808.png", False),
    34658: AbilityMeta("Water II in Blue",    "/i/003000/003809.png", False),
    34659: AbilityMeta("Blizzard II in Cyan", "/i/003000/003810.png", False),
    34660: AbilityMeta("Stone II in Yellow",  "/i/003000/003811.png", False),
    34661: AbilityMeta("Thunder II in Magenta", "/i/003000/003812.png", False),
    34662: AbilityMeta("Holy in White",       "/i/003000/003813.png", False),
    34663: AbilityMeta("Comet in Black",      "/i/003000/003814.png", False),
    34664: AbilityMeta("Pom Motif",           "/i/003000/003815.png", False),
    34665: AbilityMeta("Wing Motif",          "/i/003000/003816.png", False),
    34666: AbilityMeta("Claw Motif",          "/i/003000/003817.png", False),
    34667: AbilityMeta("Maw Motif",           "/i/003000/003818.png", False),
    34668: AbilityMeta("Hammer Motif",        "/i/003000/003819.png", False),
    34669: AbilityMeta("Starry Sky Motif",    "/i/003000/003820.png", False),
    34670: AbilityMeta("Pom Muse",            "/i/003000/003821.png", True),
    34671: AbilityMeta("Winged Muse",         "/i/003000/003822.png", True),
    34672: AbilityMeta("Clawed Muse",         "/i/003000/003823.png", True),
    34673: AbilityMeta("Fanged Muse",         "/i/003000/003824.png", True),
    34674: AbilityMeta("Striking Muse",       "/i/003000/003825.png", True),
    34675: AbilityMeta("Starry Muse",         "/i/003000/003826.png", True),
    34676: AbilityMeta("Mog of the Ages",     "/i/003000/003827.png", True),
    34677: AbilityMeta("Retribution of the Madeen", "/i/003000/003828.png", True),
    34678: AbilityMeta("Hammer Stamp",        "/i/003000/003829.png", False),
    34679: AbilityMeta("Hammer Brush",        "/i/003000/003830.png", False),
    34680: AbilityMeta("Polishing Hammer",    "/i/003000/003831.png", False),
    34681: AbilityMeta("Star Prism",          "/i/003000/003832.png", False),
    34682: AbilityMeta("Star Prism",          "/i/000000/000405.png", True),
    34683: AbilityMeta("Subtractive Palette", "/i/003000/003833.png", True),
    34684: AbilityMeta("Smudge",              "/i/003000/003834.png", True),
    34685: AbilityMeta("Tempera Coat",        "/i/003000/003835.png", True),
    34686: AbilityMeta("Tempera Grassa",      "/i/003000/003836.png", True),
    34688: AbilityMeta("Rainbow Drip",        "/i/003000/003838.png", False),

    # Summoner (17th job, the first pet-cycle caster) — names/icons probed live
    # from XIVAPI 2026-07-03; every player id below appears in real top-pull
    # cast streams (scripts/probe_summoner_ids.py). is_ogcd follows the job's
    # truth (jobs/summoner/data.py OGCD_IDS): the demi/primal summons, rites,
    # Cyclone/Strike, Slipstream and Ruin III/IV are genuinely GCDs. The pet
    # damage ids (Wyrmwave/Akh Morn/... — source = the pet actor) are flagged
    # True: they never occupy a player GCD slot, so the pacing detectors must
    # not count them as one.
    173:   AbilityMeta("Resurrection",        "/i/000000/000511.png", False),
    190:   AbilityMeta("Physick",             "/i/000000/000518.png", False),
    3578:  AbilityMeta("Painflare",           "/i/002000/002681.png", True),
    3579:  AbilityMeta("Ruin III",            "/i/002000/002682.png", False),
    3582:  AbilityMeta("Deathflare",          "/i/002000/002685.png", True),
    7426:  AbilityMeta("Ruin IV",             "/i/002000/002686.png", False),
    7427:  AbilityMeta("Summon Bahamut",      "/i/002000/002691.png", False),
    7428:  AbilityMeta("Wyrmwave",            "/i/002000/002692.png", True),
    7429:  AbilityMeta("Enkindle Bahamut",    "/i/002000/002693.png", True),
    7449:  AbilityMeta("Akh Morn",            "/i/002000/002694.png", True),
    16508: AbilityMeta("Energy Drain",        "/i/000000/000514.png", True),
    16510: AbilityMeta("Energy Siphon",       "/i/002000/002697.png", True),
    16514: AbilityMeta("Fountain of Fire",    "/i/002000/002735.png", False),
    16515: AbilityMeta("Brand of Purgatory",  "/i/002000/002736.png", False),
    16516: AbilityMeta("Enkindle Phoenix",    "/i/002000/002737.png", True),
    16518: AbilityMeta("Revelation",          "/i/002000/002732.png", True),
    16519: AbilityMeta("Scarlet Flame",       "/i/002000/002733.png", True),
    25799: AbilityMeta("Radiant Aegis",       "/i/002000/002750.png", True),
    25801: AbilityMeta("Searing Light",       "/i/002000/002780.png", True),
    25820: AbilityMeta("Astral Impulse",      "/i/002000/002757.png", False),
    25821: AbilityMeta("Astral Flare",        "/i/002000/002758.png", False),
    25823: AbilityMeta("Ruby Rite",           "/i/002000/002760.png", False),
    25824: AbilityMeta("Topaz Rite",          "/i/002000/002761.png", False),
    25825: AbilityMeta("Emerald Rite",        "/i/002000/002762.png", False),
    25826: AbilityMeta("Tri-disaster",        "/i/002000/002763.png", False),
    25830: AbilityMeta("Rekindle",            "/i/002000/002764.png", True),
    25831: AbilityMeta("Summon Phoenix",      "/i/002000/002765.png", False),
    25832: AbilityMeta("Ruby Catastrophe",    "/i/002000/002766.png", False),
    25833: AbilityMeta("Topaz Catastrophe",   "/i/002000/002767.png", False),
    25834: AbilityMeta("Emerald Catastrophe", "/i/002000/002768.png", False),
    25835: AbilityMeta("Crimson Cyclone",     "/i/002000/002769.png", False),
    25836: AbilityMeta("Mountain Buster",     "/i/002000/002770.png", True),
    25837: AbilityMeta("Slipstream",          "/i/002000/002771.png", False),
    25838: AbilityMeta("Summon Ifrit II",     "/i/002000/002772.png", False),
    25839: AbilityMeta("Summon Titan II",     "/i/002000/002773.png", False),
    25840: AbilityMeta("Summon Garuda II",    "/i/002000/002774.png", False),
    25852: AbilityMeta("Inferno",             "/i/000000/000405.png", True),
    25853: AbilityMeta("Earthen Fury",        "/i/000000/000405.png", True),
    25854: AbilityMeta("Aerial Blast",        "/i/000000/000405.png", True),
    25885: AbilityMeta("Crimson Strike",      "/i/002000/002779.png", False),
    36990: AbilityMeta("Necrotize",           "/i/002000/002699.png", True),
    36991: AbilityMeta("Searing Flash",       "/i/002000/002781.png", True),
    36992: AbilityMeta("Summon Solar Bahamut", "/i/002000/002782.png", False),
    36993: AbilityMeta("Luxwave",             "/i/002000/002783.png", True),
    36994: AbilityMeta("Umbral Impulse",      "/i/002000/002784.png", False),
    36995: AbilityMeta("Umbral Flare",        "/i/002000/002785.png", False),
    36996: AbilityMeta("Sunflare",            "/i/002000/002786.png", True),
    36997: AbilityMeta("Lux Solaris",         "/i/002000/002787.png", True),
    36998: AbilityMeta("Enkindle Solar Bahamut", "/i/002000/002788.png", True),
    36999: AbilityMeta("Exodus",              "/i/002000/002789.png", True),

    # Dark Knight (18th job, the fourth tank) — names/icons probed live from
    # XIVAPI 2026-07-05; every player id below appears in real top-pull cast
    # streams (scripts/probe_darkknight_ids.py). is_ogcd follows the job's truth
    # (jobs/darkknight/data.py OGCD_IDS): the Delirium chain (Scarlet/
    # Comeuppance/Torcleaver/Impalement), Disesteem and Unmend are genuinely
    # GCDs. Esteem's pet damage ids (source = the Living Shadow pet actor,
    # folded onto the summon cast for scoring) are flagged True — they never
    # occupy a player GCD slot, so the pacing detectors must not count them as
    # one (XIVAPI mis-categorizes 17909/36933 as weaponskills; corrected here).
    3617:  AbilityMeta("Hard Slash",          "/i/003000/003051.png", False),
    3621:  AbilityMeta("Unleash",             "/i/003000/003063.png", False),
    3623:  AbilityMeta("Syphon Strike",       "/i/003000/003054.png", False),
    3624:  AbilityMeta("Unmend",              "/i/003000/003062.png", False),
    3629:  AbilityMeta("Grit",                "/i/003000/003070.png", True),
    3632:  AbilityMeta("Souleater",           "/i/003000/003055.png", False),
    3634:  AbilityMeta("Dark Mind",           "/i/003000/003076.png", True),
    3636:  AbilityMeta("Shadow Wall",         "/i/003000/003075.png", True),
    3638:  AbilityMeta("Living Dead",         "/i/003000/003077.png", True),
    3639:  AbilityMeta("Salted Earth",        "/i/003000/003066.png", True),
    3641:  AbilityMeta("Abyssal Drain",       "/i/003000/003064.png", True),
    3643:  AbilityMeta("Carve and Spit",      "/i/003000/003058.png", True),
    7390:  AbilityMeta("Delirium",            "/i/003000/003078.png", True),
    7391:  AbilityMeta("Quietus",             "/i/003000/003079.png", False),
    7392:  AbilityMeta("Bloodspiller",        "/i/003000/003080.png", False),
    7393:  AbilityMeta("The Blackest Night",  "/i/003000/003081.png", True),
    16468: AbilityMeta("Stalwart Soul",       "/i/003000/003084.png", False),
    16469: AbilityMeta("Flood of Shadow",     "/i/003000/003085.png", True),
    16470: AbilityMeta("Edge of Shadow",      "/i/003000/003086.png", True),
    16471: AbilityMeta("Dark Missionary",     "/i/003000/003087.png", True),
    16472: AbilityMeta("Living Shadow",       "/i/003000/003088.png", True),
    17904: AbilityMeta("Abyssal Drain",       "/i/000000/000405.png", True),
    17908: AbilityMeta("Edge of Shadow",      "/i/000000/000405.png", True),
    17909: AbilityMeta("Bloodspiller",        "/i/000000/000405.png", True),
    25754: AbilityMeta("Oblation",            "/i/003000/003089.png", True),
    25755: AbilityMeta("Salt and Darkness",   "/i/003000/003090.png", True),
    25757: AbilityMeta("Shadowbringer",       "/i/003000/003091.png", True),
    25881: AbilityMeta("Shadowbringer",       "/i/000000/000405.png", True),
    32067: AbilityMeta("Release Grit",        "/i/003000/003092.png", True),
    36926: AbilityMeta("Shadowstride",        "/i/003000/003093.png", True),
    36927: AbilityMeta("Shadowed Vigil",      "/i/003000/003094.png", True),
    36928: AbilityMeta("Scarlet Delirium",    "/i/003000/003095.png", False),
    36929: AbilityMeta("Comeuppance",         "/i/003000/003096.png", False),
    36930: AbilityMeta("Torcleaver",          "/i/003000/003097.png", False),
    36931: AbilityMeta("Impalement",          "/i/003000/003098.png", False),
    36932: AbilityMeta("Disesteem",           "/i/003000/003099.png", False),
    36933: AbilityMeta("Disesteem",           "/i/000000/000405.png", True),
}

_lock = threading.Lock()
_disk_cache: Optional[dict[int, AbilityMeta]] = None
# IDs whose XIVAPI lookup failed (unknown / fabricated FFLogs IDs, or a
# transient network error). Kept in-memory only — never persisted — so a
# failed fetch isn't retried on every call. Without this, an unknown ID
# re-hits the network from the hot scoring path (score_delivered_potency
# calls get_metadata per cast), which dominated test runtime and hammers
# XIVAPI in production. Cleared on process restart so transient failures
# get another chance.
_negative_cache: set[int] = set()


def _load_disk_cache() -> dict[int, AbilityMeta]:
    global _disk_cache
    if _disk_cache is not None:
        return _disk_cache
    # Migrate any legacy ~/.fflogs_mch_compare/ contents into the new
    # dir before the first read so a returning user's cache survives.
    ensure_config_dir_migrated()
    try:
        if _CACHE_PATH.exists():
            raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            _disk_cache = {
                int(k): AbilityMeta(v["name"], v["icon"], bool(v.get("is_ogcd", False)))
                for k, v in raw.items()
            }
        else:
            _disk_cache = {}
    except Exception:
        log.exception("ability_metadata: disk cache load failed; starting empty")
        _disk_cache = {}
    return _disk_cache


def _persist_disk_cache() -> None:
    if _disk_cache is None:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        out = {
            str(k): {"name": v.name, "icon": v.icon, "is_ogcd": v.is_ogcd}
            for k, v in _disk_cache.items()
        }
        _CACHE_PATH.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        log.exception("ability_metadata: disk cache write failed")


def get_metadata(ability_id: int) -> Optional[AbilityMeta]:
    if ability_id <= 0:
        return None
    if ability_id in BUNDLED:
        return BUNDLED[ability_id]
    cache = _load_disk_cache()
    if ability_id in cache:
        return cache[ability_id]
    if ability_id in _negative_cache:
        return None
    fetched = _fetch_from_xivapi(ability_id)
    if fetched:
        with _lock:
            cache[ability_id] = fetched
            _persist_disk_cache()
    else:
        with _lock:
            _negative_cache.add(ability_id)
    return fetched


def _fetch_from_xivapi(ability_id: int) -> Optional[AbilityMeta]:
    try:
        url = f"{_XIVAPI_BASE}/action/{ability_id}?columns=Name,Icon,ActionCategory.Name"
        r = _SESSION.get(url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        name = data.get("Name") or ""
        icon = data.get("Icon") or ""
        cat = ((data.get("ActionCategory") or {}).get("Name") or "")
        if not name or not icon:
            return None
        return AbilityMeta(name=name, icon=icon, is_ogcd=(cat == "Ability"))
    except Exception as e:
        log.warning("XIVAPI fetch failed for action %s: %s", ability_id, e)
        return None
