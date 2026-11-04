"""event_lite.py - small, dependency-free headline classifier (no LLM, no network, standard library only).

This is the offline fallback for the places that used to depend on an LLM / keyword guess:
  * event/main.py   : which kind of event is this headline (results, dividend, bulk deal ...)
  * news/main.py    : sentiment of a headline when Hugging Face has no key, is paused, or fails
  * news_quality.py : sentiment of the multi-source news score

classify(headline, aliases=()) -> {type, pol, strength, about_company, is_new_info, confidence, reason}
  type   : results | board_meeting | bulk_block | insider | dividend | corporate_action | guidance |
           regulatory | management_change | rating_change | legal | other | none
  pol    : +1 good for the share price, -1 bad, 0 neutral / unclear
  strength, confidence : 0..1.  Unsure => pol 0 and confidence < MIN_CONF (callers treat that as "no opinion").
Pure function, never raises (type 'none', confidence 0 on any error).

Labelling convention (the data sets in tests/event_lite_data.py follow the same one):
  * orders / contracts / capex / USFDA approvals / launches / MoUs are type "guidance" (a business update that moves expectations)
  * bonus, buy-back = corporate_action +1; QIP, rights issue, preferential issue = corporate_action -1 (dilution); split, merger, NCD = 0
  * "board meeting / AGM to consider X" is board_meeting 0 whatever X is
  * a plant fire / grounded aircraft / bare share-price move is type "other" with the polarity of the words
  * a denied rumour = 0; a petition dismissed AGAINST the company = +1
  * a market wrap or a "stocks to watch" list is type "none" and not about one company

Copy rule: the service root file `event_lite.py` is the master. `event/event_lite.py` and `news/event_lite.py` are byte-identical
copies (each sub-app has its own Dockerfile that copies only its own folder); tests/test_event_lite.py fails when they differ.
Env: EVENT_LITE_MIN_CONF (0.5) - below this confidence sentiment() answers 0.0.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Dict, Iterable, Optional, Tuple

__all__ = ["classify", "sentiment", "event_category", "stats", "reset_stats", "TYPES"]

TYPES = (
    "results", "board_meeting", "bulk_block", "insider", "dividend", "corporate_action", "guidance",
    "regulatory", "management_change", "rating_change", "legal", "other", "none",
)

_MAX_LEN = 400


def _min_conf() -> float:
    try:
        v = float((os.getenv("EVENT_LITE_MIN_CONF") or "").strip() or 0.5)
        return v if 0.0 <= v <= 1.0 else 0.5
    except ValueError:
        return 0.5


def _rx(*parts: str) -> "re.Pattern[str]":
    return re.compile("|".join(parts), re.I)


# ── text normalisation ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _norm(s: Any) -> str:
    t = str(s or "")[:_MAX_LEN].lower()
    t = t.replace("\u20b9", " rs ").replace("&amp;", "&").replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"[\u2013\u2014]", "-", t)
    return " ".join(t.split())


# ── cost words: a cost going DOWN is good, going UP is bad (the reverse of a profit) ─────────────────────────────────────────
_COST = r"(?:input costs?|raw[- ]material costs?|costs?|expenses?|npas?|gnpas?|provisions?|provisioning|debt|borrowings?|inflation|crude(?: oil)?(?: prices?)?|delinquenc(?:y|ies)|slippages?|credit costs?)"
_UPV = r"(?:rise[sn]?|rose|jump(?:s|ed)?|surge[sd]?|spike[sd]?|soar(?:s|ed)?|climb(?:s|ed)?|increase[sd]?|swell(?:s|ed)?|widen(?:s|ed)?|badh\w*|up)"
_DNV = r"(?:fall(?:s|en)?|fell|drop(?:s|ped)?|dip(?:s|ped)?|decline[sd]?|declining|slip(?:s|ped)?|ease[sd]?|cool(?:s|ed)?|narrow(?:s|ed)?|shrink(?:s)?|shrank|cut|reduce[sd]?|ghat\w*|down)"
_COST_UP = re.compile(rf"\b{_COST}\s+(?:\w+\s+)?{_UPV}\b|\b{_UPV}\s+in\s+{_COST}\b|\b(?:higher|rising|elevated|soaring|increased|climbing)\s+(?:\w+\s+){{0,2}}{_COST}\b")
_COST_DN = re.compile(rf"\b{_COST}\s+(?:\w+\s+)?{_DNV}\b|\b{_DNV}\s+in\s+{_COST}\b|\b(?:lower|falling|softer|reduced|easing|declining)\s+(?:\w+\s+)?{_COST}\b")
_LOSS_NARROW = re.compile(r"\b(?:loss(?:es)?)\s+(?:\w+\s+)?(?:narrow\w*|shrink\w*|shrank|reduc\w+|declin\w+|fall\w*|fell)\b|\bnarrower loss\b|\bswings? to (?:a )?(?:net )?profit\b|\bswings? to (?:the )?black\b|\bturns? (?:to )?(?:the )?black\b|\bturns? (?:to )?profit(?:able)?\b|\breturns? to profit\b")
_LOSS_WIDEN = re.compile(r"\b(?:loss(?:es)?)\s+(?:\w+\s+)?(?:widen\w*|deepen\w*|balloon\w*|rise[sn]?|jump\w*|swell\w*)\b|\bwider loss\b|\bswings? to (?:a )?(?:net )?loss\b|\bslips? into loss\b|\bturns? loss[- ]making\b")
_GUIDE_CUT = re.compile(r"\b(?:lowers?|cuts?|trims?|slashes|slashed|reduces?|pares?|downgrades?)\s+(?:its\s+|the\s+)?(?:(?:fy|full[- ]year|annual)\s*\d*\s+)?(?:[a-z-]+\s+){0,3}(?:outlook|guidance|forecasts?|growth|estimates?)\b")
_GUIDE_RAISE = re.compile(r"\b(?:raises?|lifts?|hikes?|boosts?|ups|increases?)\s+(?:its\s+|the\s+)?(?:(?:fy|full[- ]year|annual)\s*\d*\s+)?(?:[a-z-]+\s+){0,3}(?:outlook|guidance|forecasts?)\b")
_WEAK_GROWTH = re.compile(r"\b(?:growth|sales|revenue|volumes?|demand)\s+(?:is\s+|remains?\s+|stays?\s+)?(?:flat|muted|sluggish|subdued|weak|slows?|slowed|slowdown|stagnant)\b|\bflat (?:growth|sales|revenue|volumes?)\b")


_FAILS_TO = re.compile(r"\b(?:fails?|failed|unable|struggles?) to (?:\w+ )?(?:lift|boost|impress|excite|cheer|help|inspire|support)\w*\b")
_PAST_MOVE = re.compile(r"\bafter (?:a |an )?\d+(?:\.\d+)?%\s+(?:[\w-]+\s+)?(?:fall|drop|decline|slump|slide|correction|decline)\b|\b(?:is )?the worst (?:over|behind)\b|\bworst is over\b")


def _mask(t: str) -> str:
    t = _PAST_MOVE.sub(" ", t)
    t = _FAILS_TO.sub(" failslift ", t)
    t = _LOSS_NARROW.sub(" lossnarrows ", t)
    t = _LOSS_WIDEN.sub(" losswidens ", t)
    t = _COST_UP.sub(" costup ", t)
    t = _COST_DN.sub(" costdown ", t)
    t = _WEAK_GROWTH.sub(" weakgrowth ", t)
    t = _GUIDE_CUT.sub(" guidecut ", t)
    t = _GUIDE_RAISE.sub(" guideraise ", t)
    return t


# ── polarity lexicon (weight per hit) ──────────────────────────────────────────────────────────────────────────────────────────
_POS: Tuple[Tuple["re.Pattern[str]", float], ...] = (
    (_rx(r"\blossnarrows\b", r"\bcostdown\b", r"\bguideraise\b"), 2.0),
    (_rx(r"\bbeats?\b(?! the odds)", r"\bsurpass(?:es|ed)?\b", r"\boutperform\w*", r"\brecord\b", r"\bdoubles\b", r"\btriples\b", r"\bsoars?\b", r"\bsurges?\b", r"\bjumps?\b",
         r"\bclimbs?\b", r"\brall(?:y|ies|ied)\b", r"\bspikes?\b", r"\bzoom(?:s|ed)?\b", r"\bturnaround\b", r"\bupgrade[sd]?\b", r"\bnods?\b", r"\bbags?\b", r"\bsecures?\b", r"\bwins?\b", r"\bwon\b",
         r"\bbadh[ae]\b", r"\bbadhi\b", r"\buchh?al\w*", r"\btezi\b", r"\bbehtar\b", r"\bmazboot\b", r"\bmunafa badha\b", r"\bsahara\b", r"\bmila\b"), 1.4),
    (_rx(r"\brises?\b", r"\brose\b", r"\bgains?\b", r"\bgained\b", r"\bup\s+(?:by\s+)?\d", r"\bhikes?\b", r"\braises?\b", r"\braised\b", r"\bexpands?\b", r"\bimprov\w+",
         r"\bstrong\w*", r"\bhealthy\b", r"\bboosts?\b", r"\bboosted\b", r"\blifts?\b", r"\bapproves?\b", r"\bapproved\b", r"\bapproval\b", r"\bclears?\b", r"\bcleared\b",
         r"\bbuys?\b", r"\bbought\b", r"\bkharid\w*", r"\bpicks? up\b", r"\bacquires?\b", r"\bsigns?\b(?!\s+of\b)", r"\blaunch\w*", r"\bgrowth\b", r"\bprofit(?:able|ability)\b", r"\bpositive\b",
         r"\bbullish\b", r"\bupbeat\b", r"\bback in operations?\b", r"\bresum(?:es|ed) (?:production|operations)\b", r"\bnet interest margin (?:expands?|widens?)\b", r"\bhigher\b", r"\brecover\w*", r"\brebound\w*", r"\bgood\b", r"\bbetter\b"), 1.0),
)
_NEG: Tuple[Tuple["re.Pattern[str]", float], ...] = (
    (_rx(r"\bfailslift\b", r"\blosswidens\b", r"\bcostup\b", r"\bguidecut\b", r"\bmiss(?:es|ed)?\b(?= (?:the )?(?:street|estimates?|expectations?|forecasts?|views?|consensus))", r"\bnet loss\b", r"\bplunge[sd]?\b", r"\bcrash\w*", r"\btank(?:s|ed)?\b",
         r"\bfraud\b", r"\bdefault(?:s|ed)?\b", r"\bdowngrade[sd]?\b", r"\bwarning letter\b", r"\bform 483\b", r"\bimport alert\b", r"\bslump\w*", r"\bcollapse[sd]?\b", r"\bscam\b",
         r"\bbankrupt\w*", r"\binsolvency (?:petition|plea|proceedings?)\b", r"\bnuksan\b"), 2.0),
    (_rx(r"\bcuts?\b", r"\blowers?\b", r"\blowered\b", r"\bslash(?:es|ed)?\b", r"\bloses\b", r"\blost\b", r"\bcancel\w*", r"\bdenied\b", r"\bsuspend\w*", r"\bgrounds?\b", r"\bgrounded\b",
         r"\bghat[ae]\b", r"\bghati\b", r"\bnpas?\s+badh\w*", r"\bgir(?:e|i|a|awat)\b", r"\bkamzor\b", r"\bdabav\b", r"\bweakgrowth\b", r"\bmiss(?:es|ed)?\b", r"\bdisappoint\w*", r"\bshortfall\b", r"\bunderperform\w*",
         r"\bstakes? sale\b", r"\boffloads?\b", r"\bdumps?\b", r"\bbech[ai]\b", r"\bsells?\b", r"\bsold\b", r"\bpledg\w+", r"\bhalts?\b", r"\bhalted\b", r"\bfire\b", r"\bblaze\b", r"\bexplosion\b", r"\brecalls?\b",
         r"\bprobe\b", r"\braid\w*", r"\bchh?ap[ae]\w*", r"\bpenalt\w+", r"\bjurmana\b", r"\bfines?\b", r"\bfined\b", r"\bbans?\b", r"\bbarred\b", r"\bbars\b", r"\bcurbs?\b", r"\brestrict\w+", r"\bresign\w*", r"\bistifa\b",
         r"\bquits?\b", r"\bsteps? down\b", r"\bexits?\b", r"\bnegative\b", r"\bbearish\b", r"\bweak\w*", r"\bworst\b", r"\blower\b", r"\bslowdown\b", r"\bheadwinds?\b", r"\bconcerns?\b", r"\bwoes?\b", r"\bcrisis\b", r"\bcautious\b", r"\bcautionary\b", r"\bwary\b", r"\buncertain\w*"), 1.4),
    (_rx(r"\bfalls?\b", r"\bfell\b", r"\bfallen\b", r"\bdrops?\b", r"\bdropped\b", r"\bdips?\b", r"\bdipped\b", r"\bdeclines?\b", r"\bdeclined\b", r"\bslips?\b", r"\bslipped\b", r"\bslides?\b", r"\bslid\b",
         r"\btumbles?\b", r"\bsinks?\b", r"\bsank\b", r"\bdown\s+(?:by\s+)?\d", r"\bloss(?:es)?\b", r"\bwidens?\b", r"\bshrinks?\b", r"\bsheds?\b", r"\bunder pressure\b", r"\bdull\b", r"\bsoft\b", r"\bdelays?\b", r"\bdelayed\b",
         r"\bdip\b", r"\bsell-?off\b", r"\bdefer(?:s|red)?\b", r"\bskips?\b", r"\bomits?\b"), 1.0),
)


def _score(masked: str) -> Tuple[float, float]:
    pos = sum(w * len(rx.findall(masked)) for rx, w in _POS)
    neg = sum(w * len(rx.findall(masked)) for rx, w in _NEG)
    return pos, neg


def _direction(masked: str) -> Tuple[int, float, float, float]:
    """-> (pol, pos, neg, margin). A tie, or a margin under 1, is 0 (unclear / mixed)."""
    pos, neg = _score(masked)
    diff = pos - neg
    if abs(diff) < 1.0:
        return 0, pos, neg, abs(diff)
    return (1 if diff > 0 else -1), pos, neg, abs(diff)


# ── type patterns ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
_NONE = _rx(r"\bstocks? to (?:watch|buy|bet|focus|track)\b", r"\b(?:trading|market|exchange) holidays?\b", r"\bstocks? in (?:news|focus|spotlight)\b", r"\btop (?:gainers?|losers?|picks?|\d+ stocks?)\b", r"\bgainers? (?:and|&) losers?\b",
            r"\bmarket (?:wrap|live|update|closing|today|outlook|opening|summary)\b", r"\b(?:sensex|nifty|bank nifty|nifty ?50)\b.*\b(?:ends?|closes?|opens?|settles?|trades?|tumbles?|surges?|slips?|falls?|gains?|rises?|jumps?|rall(?:y|ies)|drags?|extends?|hits?)\b",
            r"^(?:nifty|sensex|bank nifty)\b", r"\bhow to trade\b", r"\b(?:nifty|sensex|bank ?nifty)\b.*\b(?:prediction|forecast|levels?|technical|outlook)\b", r"\b(?:prediction|technical outlook|key levels|what to expect|trading strategy|trade setup)\b",
            r"\bstocks? to (?:buy|sell) today\b", r"\bipo (?:gmp|allotment status|listing)\b", r"\bsgx nifty\b|\bgift nifty\b",
            r"^(?:rupee|dollar|us dollar|brent|crude|gold|silver)\b", r"\brupee\b.*\b(?:dollar|settles?|ends?|closes?|opens?)\b",
            r"\btechnical (?:recovery|breakout|view|picks?|signs?|setup|chart|analysis)\b",
            r"\bstock picks?\b", r"\bstock recommendations?\b", r"\bmuhurat\b|\bsamvat\b", r"\bon brokerages' radar\b", r"\b(?:weekly|monthly) (?:recap|wrap|roundup|outlook)\b", r"\boutlook for the (?:week|day|month)\b",
            r"\bstock price,? news\b", r"\bshare price\s*-\s*live\b", r"\bshare price today\b", r"\bstock price (?:&|and) chart\b", r"\blive streaming\b|\b(?:odi|t20|ipl)\b", r"\bmidterms?\b",
            r"\bwinners? and (?:potential |likely )?losers?\b|\blikely winners\b", r"\banalysts'? views?\b", r"^(?:sugar|cotton|wheat|palm oil|copper|aluminium|zinc|nickel|natural gas)\s+(?!stocks?\b|shares?\b)",
            r"\bshould you\b.*\b(?:buy|sell|hold|invest|bet)\b", r"\bbuy,? sell,? or hold\b")
# a bare "key levels" / "trade setup" tail on a results headline does not make it a market wrap (Kanohar: "surge 17% after Q2 results ... | Check key levels")
_NONE_SOFT = _rx(r"\bkey levels\b", r"\btrade setup\b", r"\bwhat to expect\b")
_RUMOUR = _rx(r"\b(?:rumou?rs?|reports?|speculation|buzz)\b.*\b(?:false|baseless|untrue|incorrect|misleading|denies?|denied|clarif\w+|dismiss\w+|no truth)\b",
              r"\b(?:denies|denied|dismisses|refutes|rubbishes|rejects)\s+(?:the\s+)?(?:media\s+)?(?:reports?|rumou?rs?|speculation|claims?)\b", r"\bbaseless\b", r"\bclarifies? on (?:media |news )?(?:reports?|speculation)\b",
              r"\bno truth\b", r"\bfalse (?:reports?|rumou?rs?)\b", r"\bafwah\b")
_TALKS = _rx(r"\bin talks?\b", r"\bis said to\b", r"\bexploring (?:options|sale|stake)\b")
_INVESTOR_BOOK = _rx(r"\b(?:kacholia|jhunjhunwala|damani|kedia|dolly khanna|singhania|porinju|veliyath)\b.*\bportfolio\b", r"\bportfolio\b.*\b(?:what's in|new stocks|list)\b")
_BOARD = _rx(r"\bboard (?:meeting|meets?|to (?:consider|meet|discuss))\b", r"\bboard of [a-z][a-z\' .&-]{1,30}? (?:to meet|meets|meeting)\b", r"\bagm\b", r"\begm\b", r"\bannual general meeting\b", r"\bextra-?ordinary general meeting\b", r"\bpostal ballot\b")
_MGMT_ROLE = r"(?:ceo|cfo|coo|cto|cio|md|chairman|chairperson|chairwoman|auditors?|directors?|managing director|chief (?:executive|financial|operating|technology|risk|business)(?: officer)?|president|company secretary|head)"
_MGMT = _rx(rf"\b{_MGMT_ROLE}\b.*\b(?:resign\w*|quits?|steps? down|stepping down|appoint\w*|names?|named|replac\w+|exits?|exit|ousted|removed|sacked|istifa|hired|elevated|promoted|taken? charge)\b",
            rf"\b(?:resign\w*|steps? down|quits?|appoints?|appointed|names?|named|hires?|exits?|replaces?)\b.*\b{_MGMT_ROLE}\b", rf"\bnew {_MGMT_ROLE}\b", r"\b(?:senior|top|key)\s+(?:executive|official|management|leader)s?\b.*\b(?:departs?|leaves?|quits?|exits?|resigns?)\b|\b(?:departure|exit) of (?:a |another )?(?:senior|top|key)\b", rf"\b{_MGMT_ROLE}\b.*\b(?:ne|ka|ke)\b.*\bistifa\b")
_RATING = _rx(r"\b(?:upgrade[sd]?|downgrade[sd]?)\b", r"\b(?:raises?|cuts?|lowers?|hikes?|slashes|boosts?|revises?|keeps?|retains?|maintains?|trims?)\s+(?:its\s+|the\s+)?(?:[a-z&.']+\s+){0,3}?target\b", r"\btarget (?:price )?(?:raised|cut|lowered|hiked|to|of)\b",
              r"\b(?:initiates?|reiterates?|resumes?|assumes?)\s+(?:coverage|buy|sell|hold)\b", r"\b(?:crisil|icra|care ratings|care|moody'?s|fitch|s&p|india ratings|ind-ra|brickwork|acuite)\b.*\b(?:rat(?:ing|es|ed)|outlook|affirms?|assigns?|upgrades?|downgrades?|reaffirms?)\b",
              r"\brating (?:is )?(?:cut|lowered|raised|upgrade[sd]?|downgrade[sd]?|affirmed|reaffirmed)\b", r"\b(?:outlook|rating)\b.*\b(?:negative|stable|positive|watch)\b.*\b(?:rating|outlook)\b", r"\b(?:overweight|underweight|outperform|underperform|top pick|accumulate)\b",
              r"\b(?:rating|ratings)\b.*\b(?:cut|lowered|raised|affirmed)\b", r"\b(?:affirms?|reaffirms?|assigns?)\b.*\brating\b", r"\bnegative outlook\b|\bpositive outlook\b",
              r"\b(?:buy|sell|hold|neutral|reduce|add)\s+(?:rating|call|recommendation)s?\b", r"\bretains?\s+(?:a\s+|its\s+)?(?:positive|buy|bullish|negative|neutral|sell)\b", r"\bratings?\s+(?:stays?|remains?)\b",
              r"\b(?:goldman sachs|jefferies|morgan stanley|nomura|jpmorgan|j\.p\. morgan|macquarie|ubs|bernstein|hsbc|bofa|bank of america|citi|clsa|brokerages?)\b.*\b(?:bearish|bullish|overweight|underweight|neutral|rating|ratings|target|retain\w*|maintain\w*)\b")
_REG = _rx(r"\bwarning letter\b", r"\bimport alert\b", r"\bform 483\b", r"\binspection observations?\b", r"\busfda\b.*\b(?:clearance|eir|clears?|cleared|observations?|inspection|violation)\b", r"\beir\b",
           r"\brecalls?\b", r"\b(?:rbi|irdai|dgca|trai|cdsco|fssai|nhai|pfrda|sebi)\b.*\b(?:penalt\w*|restrict\w*|curbs?|bars?|barred|action|ban(?:s|ned)?|fines?|fined|suspends?|suspended|cancels?|cancelled|licen[cs]e|jurmana|directions?|norms?|caps?)\b",
           r"\b(?:penalt\w*|restrict\w*|curbs?|bars?|barred|action|ban(?:s|ned)?|fines?|fined|suspends?|suspended|cancels?|licen[cs]e)\b.*\b(?:by|from|ne|se)\s+(?:the\s+)?(?:rbi|irdai|dgca|trai|cdsco|fssai|nhai)\b",
           r"\b(?:rbi|irdai|dgca|trai)\s+(?:ne|se|ka|ki|ko)\b", r"\bko\s+(?:rbi|irdai|dgca)\b")
_USFDA_APPROVAL = _rx(r"\b(?:gets?|receives?|secures?|bags?|wins?)\s+(?:a\s+|the\s+)?(?:final\s+|tentative\s+)?(?:nod|approval)\s+from\s+(?:the\s+)?(?:us|u\.s\.|usa)?\s*(?:fda|usfda|drug regulator|drug controller|health regulator|food and drug)", r"\busfda\b.*\b(?:nod|approval|approves?|approved|go-?ahead|tentative approval)\b", r"\b(?:gets?|receives?|secures?|bags?|wins?)\s+(?:final\s+|tentative\s+)?(?:usfda|fda|cdsco|dcgi|ema)\s+(?:nod|approval)\b",
                     r"\b(?:fda|cdsco|dcgi)\b.*\b(?:nod|approval|approves?|approved)\b")
_LEGAL = _rx(r"\bsebi\b.*\b(?:order|penalty|fine|ban|bars?|barred|probe|notice|show-cause|investigat\w+|adjudicat\w+)\b", r"\bshow[- ]cause\b", r"\b(?:ed|cbi|income[- ]tax(?: department)?|it department|gst|dri|sfio|enforcement directorate)\b.*\b(?:raid\w*|search\w*|probe|notice|summons?|chh?ap\w*|books?|arrests?|attach\w*|charges?|investigat\w+)\b",
             r"\braid(?:s|ed)?\b", r"\bfraud\b", r"\bpenalt(?:y|ies)\b", r"\bcourt\b", r"\bnclt\b|\bnclat\b|\bnclt\b", r"\binsolvency\b", r"\blawsuit\b|\blitigation\b|\bsued?\b", r"\bcompetition commission\b|\bcci\b",
             r"\bdefault(?:s|ed)?\b", r"\bchh?ap[ae]\w*", r"\barbitration\b", r"\btribunal\b", r"\bbribery\b|\bmoney laundering\b|\bcorruption\b", r"\bcharge-?sheet\b", r"\bsc\b.*\b(?:order|verdict)\b", r"\bsupreme court\b|\bhigh court\b",
             r"\bpetition\b", r"\bnotice\b.*\b(?:tax|authorit\w+|sebi|ed|cbi)\b|\b(?:tax|authority)\b.*\bnotice\b", r"\bcbi\b|\benforcement directorate\b",
             r"\b(?:department of justice|doj|sec|ftc)\b.*\b(?:investigations?|probe|subpoena|inquiry|charges?)\b", r"\bfacing (?:an? )?(?:investigation|probe)\b")
_BULK = _rx(r"\bbulk deals?\b", r"\bblock deals?\b", r"\bblock trades?\b", r"\bbulk (?:buy|purchase|sell|sale)\b")
_INSIDER = _rx(r"\b(?:raises?|hikes?|increases?|lifts?|cuts?|reduces?|trims?|pares?|boosts?)\s+stake\b", r"\bpromoters?\b", r"\binsiders?\b", r"\bpledg\w+", r"\bsast\b", r"\bopen[- ]market (?:purchase|sale|buy)\b", r"\bshareholding pattern\b", r"\bdirector\b.*\b(?:picks? up|buys?|sells?|acquires?)\b\s*(?:shares|stake)", r"\bkey managerial\b", r"\bpromoter group\b")
_DIVIDEND = _rx(r"\bdividends?\b", r"\bex-dividend\b", r"\bpayout\b")
_CORP = _rx(r"\bbuy-?\s?back\b", r"\bbonus\b", r"\b(?:stock|share)s? split\b|\bsplit\b", r"\brights? issue\b", r"\bqip\b", r"\bpreferential (?:issue|allotment)\b", r"\b(?:de)?merger\b|\bdemerger\b|\bamalgamat\w+", r"\bfpo\b|\bofs\b|\boffer for sale\b",
            r"\bncds?\b|\bnon-convertible\b", r"\bfund[- ]?raising\b|\braise[sd]? (?:rs|funds|capital)\b|\braises? rs\b", r"\bdelist\w*", r"\btakeover\b|\bopen offer\b", r"\bwarrants?\b", r"\bconvertible\b", r"\bdilution\b",
            r"\bcompletes?\s+(?:the\s+)?(?:acquisition|merger|takeover|buyout)\b", r"\bacquisition of\b", r"\bacqui(?:res?|sition)\b.*\b(?:stake|company|firm|business)\b|\b(?:stake|company|business)\b.*\bacqui\w+", r"\bdemerg\w+", r"\brecord date\b")
_GUIDE_EXPLICIT = _rx(r"\bguidance\b", r"\boutlook\b", r"\bforecast\b", r"\bprojection\b", r"\bexpects?\b.*\b(?:growth|sales|revenue|margins?|volumes?)\b", r"\btargets?\b.*\b(?:revenue|sales|growth|capacity)\b")
_GUIDE = _rx(r"\borders?\b", r"\bcontracts?\b", r"\bmou\b", r"\bcapex\b", r"\bcapital expenditure\b", r"\bexpansions?\b", r"\bnew (?:project|plant|facility|unit|launch|product|capacity)\b", r"\blaunch(?:es|ed)?\b",
             r"\binvest(?:s|ment)?\b", r"\bcapacity\b", r"\bwins?\b.*\b(?:deal|bid|tender|project)\b", r"\b(?:deal|bid|tender|project|ppa|power purchase agreement|partnership|tie-?up|pact)\b", r"\bbags?\b", r"\bsecures?\b",
             r"\bsigns?\b(?!\s+of\b)", r"\bawarded\b|\bawards?\b", r"\border (?:book|inflow)s?\b", r"\bmila\b", r"\bpipeline\b", r"\bl1 bidder\b|\blowest bidder\b")
_RESULT_STRONG = _rx(r"\bq[1-4]\b", r"\b[1-4]q\b", r"\bq[1-4]fy\d*\b", r"\bh[12]\b", r"\bquarter(?:ly)?\b", r"\bresults?\b", r"\bearnings?\b", r"\bpat\b", r"\bnet profit\b", r"\bnet loss\b", r"\bfy\d{2}\b.*\b(?:profit|loss|pat)\b",
                     r"\b(?:profit|loss)\b.*\b(?:rises?|falls?|jumps?|declines?|surges?|doubles?|narrows?|widens?|slumps?|climbs?|up|down|drops?|dips?|grows?|soars?|plunges?)\b",
                     r"\b(?:rises?|falls?|jumps?|declines?|surges?|doubles?|narrows?|widens?|slumps?|climbs?|drops?|dips?|grows?|soars?|plunges?)\b.*\b(?:profit|loss|pat|ebitda)\b",
                     r"\bswings? to (?:a )?(?:profit|loss)\b", r"\bnet\s+(?:rises?|rose|jumps?|surges?|falls?|fell|declines?|drops?|slumps?|climbs?|grows?|up|down)\b", r"\bebitda\b", r"\bnet income\b", r"\bmunafa\b", r"\bnateeje\b|\bnatije\b", r"\bprofit\b.*\b(?:slump|surge|crash)\b", r"\bposts? (?:a )?(?:profit|loss)\b|\breports? (?:a )?(?:profit|loss)\b")
_ACCIDENT = _rx(r"\bfire\b", r"\bblaze\b", r"\bexplosion\b", r"\baccident\b", r"\bgrounds?\b", r"\bgrounded\b", r"\bhalts? (?:production|operations)\b", r"\bstrike\b", r"\bshutdown\b|\bshut down\b", r"\bplant (?:shut|closed|closure)\b", r"\bcasualt\w+", r"\bflood\b")
_MOVE = _rx(r"\b(?:shares?|stock|scrip|stocks)\b.*\b(?:falls?|slides?|jumps?|rall(?:y|ies)|gains?|surges?|tumbles?|slips?|drops?|sinks?|soars?|climbs?|rises?|plunges?|dips?|girawat|gire|tezi|uchh?al\w*)\b",
            r"\b(?:falls?|slides?|jumps?|rall(?:y|ies)|gains?|surges?|tumbles?|slips?|drops?|sinks?|soars?|climbs?|rises?|plunges?)\s+\d")

_NO_NEW_INFO = _rx(r"\b(?:preview|what to expect|ahead of|expected to|likely to|may announce|set to announce|to watch|stocks? to|explained|explainer|analysis|opinion|why (?:is|are|did|has)|should you)\b", r"\bhow to\b", r"\bfaq\b|\bq&a\b")

_RESULT_FORCE_DIV = _rx(r"\bq[1-4]\b", r"\bquarter(?:ly)?\b", r"\bresults?\b", r"\bearnings?\b", r"\bpat\b")
_GENERIC_ALIAS_WORDS = frozenset({"bank", "limited", "ltd", "india", "indian", "industries", "finance", "financial", "services", "corp", "corporation", "company", "co", "group", "enterprises", "holdings", "the", "of", "and", "&", "tata"})

_BASE_STRENGTH = {
    "results": 0.8, "legal": 0.75, "regulatory": 0.7, "guidance": 0.6, "corporate_action": 0.6, "management_change": 0.6, "bulk_block": 0.5,
    "insider": 0.5, "rating_change": 0.5, "dividend": 0.45, "board_meeting": 0.2, "other": 0.3, "none": 0.0,
}


# ── per-type polarity ──────────────────────────────────────────────────────────────────────────────────────────────────────────
_RELIEF = _rx(r"\bdismiss\w*", r"\breject\w*", r"\bquash\w*", r"\bstays?\b", r"\bstayed\b", r"\brelief\b", r"\bclean chit\b", r"\bacquit\w*", r"\bclears?\b", r"\bcleared\b", r"\bupholds?\b.*\bfavou?r\b", r"\bin favou?r\b", r"\bwins? (?:case|appeal|battle)\b", r"\bsets? aside\b")
_AGAINST = _rx(r"\bagainst\b")
_NEG_ACTION_BY_COURT = _rx(r"\badmits?\b", r"\bupholds?\b", r"\bseizes?\b", r"\battach\w+", r"\bconvicts?\b", r"\bpenalis\w+|\bpenaliz\w+")


def _pol_legal(t: str, m: str) -> Tuple[int, float]:
    if _RELIEF.search(t) and (_AGAINST.search(t) or re.search(r"\brelief\b|\bclean chit\b|\bacquit|\bclears?\b|\bcleared\b|\bin favou?r\b|\bquash|\bset[s]? aside\b|\bwins\b", t)) and not _NEG_ACTION_BY_COURT.search(t):
        return 1, 0.85
    return -1, 0.85


_SELLERS = _rx(r"\b(?:sells?|sold|offloads?|dumps?|exits?|trims?|cuts? stake|reduces? stake|pares?|bech[ai]|becha|bechi|sale)\b", r"\bpledg\w+", r"\bstake sale\b", r"\bpledge\b")
_BUYERS = _rx(r"\b(?:buys?|bought|picks? up|acquires?|purchases?|purchased|raises? stake|hikes? stake|increases? stake|adds?|kharid\w*|kharide|kharidi|boosts? stake|lifts? stake)\b", r"\b(?:open[- ]market )?purchase\b", r"\bbuying\b|\baccumulat\w+")


def _pol_flow(t: str) -> Tuple[int, float]:
    """bulk / block / promoter flow: who bought, who sold."""
    b = bool(_BUYERS.search(t))
    s = bool(_SELLERS.search(t))
    if b and not s:
        return 1, 0.8
    if s and not b:
        return -1, 0.8
    if b and s:
        return 0, 0.4
    return 0, 0.5


def _pol_mgmt(t: str) -> Tuple[int, float]:
    if re.search(r"\b(?:resign\w*|quits?|steps? down|stepping down|exits?|departs?|departure|leaves|ousted|removed|sacked|istifa|replaced|replaces|resignation)\b", t):
        return -1, 0.8
    if re.search(r"\b(?:appoint\w*|names?|named|hires?|elevated|promoted|taken? charge|new (?:ceo|cfo|md|chairman))\b", t):
        return 0, 0.7
    return 0, 0.45


def _pol_rating(t: str, m: str) -> Tuple[int, float]:
    if re.search(r"\bless (?:bearish|negative|cautious)\b", t):
        return 0, 0.6          # a softer stance that still keeps a Sell / Underweight: mixed
    if re.search(r"\b(?:turns?|turned|becomes?)\s+(?:more\s+)?(?:bullish|positive|upbeat)\b|\bmore (?:bullish|positive)\b", t):
        return 1, 0.75
    if re.search(r"\b(?:turns?|turned|becomes?)\s+(?:more\s+)?(?:bearish|negative|cautious)\b|\bmore (?:bearish|negative|cautious)\b", t):
        return -1, 0.75
    if re.search(r"\b(?:retains?|maintains?|keeps?)\b", t) and re.search(r"\b(?:positive|bullish|buy|overweight|outperform)\b", t) and not re.search(r"\b(?:affirms?|reaffirms?|outlook|downgrade|cuts?|lowers?)\b", t):
        return 1, 0.75
    if re.search(r"\b(?:affirms?|reaffirms?|retains?|maintains?|keeps?)\b", t) and not re.search(r"\b(?:downgrade|upgrade|cuts?|raises?|lowers?)\b", t):
        if re.search(r"\bnegative\b", t) and not re.search(r"\bstable\b", t):
            return -1, 0.65
        return 0, 0.7
    if re.search(r"\b(?:downgrade[sd]?|cuts?|lowers?|lowered|slashe?s?|trims?|negative outlook|underweight|underperform|reduce|sell|rating cut|rating (?:is )?(?:cut|lowered))\b", t) and not re.search(r"\bupgrade[sd]?\b|\braises? target\b", t):
        return -1, 0.85
    if re.search(r"\b(?:upgrade[sd]?|raises?|hikes?|boosts?|overweight|outperform|top pick|accumulate|initiates?|buy|positive outlook|rating (?:is )?raised)\b", t) and not re.search(r"\bdowngrade[sd]?\b|\bcuts?\b|\blowers?\b", t):
        return 1, 0.85
    pol, _, _, _ = _direction(m)
    return pol, 0.5


def _pol_reg(t: str, m: str) -> Tuple[int, float]:
    if re.search(r"\b(?:lifts?|lifted|removes?|removed|eases?|eased|relaxes?|relaxed|revokes?|revoked|withdraws?|withdrew)\s+(?:the\s+|its\s+|all\s+)?(?:\w+\s+)?(?:restrictions?|curbs?|ban|bar|penalt\w+|embargo|prohibition|sanctions?)\b", t):
        return 1, 0.8
    if re.search(r"\bno (?:\w+ )?(?:impact|effect)\b|\bunaffected\b|\bnot affected\b", t):
        return 0, 0.65
    if re.search(r"\b(?:clearance|clears?|cleared|eir (?:issued|received)|eir\b|lifts?|lifted|revokes?|withdraws?|relaxes?|relief|nod)\b", t) and not re.search(r"\b(?:warning|483|import alert|penalt|fine|ban|curb|restrict|suspend|cancel)\w*", t):
        return 1, 0.8
    if re.search(r"\b(?:dismiss\w*|denies|denied)\b", t) and re.search(r"\b(?:report|rumou?r)\b", t):
        return 0, 0.6
    return -1, 0.82


def _pol_div(t: str, m: str) -> Tuple[int, float]:
    p, c = _pol_div0(t, m)
    if p > 0 and re.search(r"\b(?:profit|loss|revenue|sales|pat|earnings)\b", t) and _direction(m)[0] < 0:
        return 0, 0.6        # a dividend next to bad numbers: mixed
    return p, c


def _pol_div0(t: str, m: str) -> Tuple[int, float]:
    if re.search(r"\b(?:skips?|skipped|omits?|omitted|no dividend|cuts? dividend|slashe?s? dividend|defers?|withholds?|halts? dividend|dividend cut|dividend skipped)\b", t):
        return -1, 0.8
    if re.search(r"\b(?:record date|ex-dividend|ex-date|ex date)\b", t) and not re.search(r"\b(?:declares?|announces?|recommends?|ghoshit|elaan|special|hikes?|raises?)\b", t):
        return 0, 0.6
    if re.search(r"\b(?:declares?|announces?|recommends?|approves?|ghoshit|elaan|special|hikes?|raises?|pays?|payout)\b", t):
        return 1, 0.8
    return 1, 0.55


def _pol_corp(t: str, m: str) -> Tuple[int, float]:
    if re.search(r"\b(?:qip|rights? issue|preferential (?:issue|allotment)|fpo|ofs|offer for sale|dilution|warrants?|fund[- ]?raising|raise[sd]? (?:rs|funds|capital))\b", t) and not re.search(r"\bbuy-?\s?back\b|\bbonus\b", t):
        if re.search(r"\bncds?\b|\bbonds?\b|\bdebentures?\b|\bat1\b|\btier[- ]?[12]\b", t):
            return 0, 0.7
        return -1, 0.75
    if re.search(r"\bbuy-?\s?back\b|\bbonus\b|\bbuy back\b", t):
        return 1, 0.85
    if re.search(r"\b(?:split|merger|demerger|amalgamat\w+|ncds?|non-convertible|record date|demerg\w+|delist\w*|takeover|open offer|convertible|acqui\w+)\b", t):
        return 0, 0.7
    pol, _, _, _ = _direction(m)
    return pol, 0.5


def _pol_guidance(t: str, m: str) -> Tuple[int, float]:
    if re.search(r"\b(?:loses|lost|cancel\w*|scrap\w*|withdraw\w*|terminat\w+|delay\w*|defer\w*|put on hold|deferred|slows?|loses out)\b", t) and re.search(r"\b(?:order|contract|deal|bid|tender|project|ppa|agreement|capex)\b", t):
        return -1, 0.8
    pol, pos, neg, margin = _direction(m)
    if pol != 0:
        return pol, min(0.9, 0.62 + 0.1 * margin)
    if _USFDA_APPROVAL.search(t):
        return 1, 0.8
    if _GUIDE.search(t) and not re.search(r"\b(?:cuts?|lowers?|loses|lost|slash\w*|delay\w*)\b", t):
        return 1, 0.62
    return 0, 0.45


def _pol_results(t: str, m: str) -> Tuple[int, float]:
    pol, pos, neg, margin = _direction(m)
    if re.search(r"\b(?:beats?|tops?|surpass\w*)\b", t) and re.search(r"\bmiss(?:es|ed)?\b", t):
        return 0, 0.6        # one line beats, another misses: mixed (checked first, before the single-miss rule)
    # "profit rises but misses estimates": a miss against the Street beats the rise
    if re.search(r"\bmiss(?:es|ed)?\b.*\b(?:estimates?|expectations?|street|forecasts?|consensus)\b|\bfall(?:s)? short of\b|\bbelow (?:estimates?|expectations?|street)\b", t):
        return -1, 0.82
    if re.search(r"\b(?:beats?|tops?|surpass\w*|above|ahead of)\b.*\b(?:estimates?|expectations?|street|forecasts?|consensus)\b", t) and neg < 2.0:
        return 1, 0.82
    if re.search(r"\b(?:beats?|tops?|surpass\w*)\b", t) and re.search(r"\bmiss(?:es|ed)?\b", t):
        return 0, 0.6        # one line beats, another misses: mixed
    if pol != 0 and re.search(r"\bdespite\b", t) and pos > 0 and neg > 0:
        return 0, 0.6        # "shares decline despite loan growth": one line good, one bad = mixed
    if pol == 0:
        return 0, 0.5
    return pol, min(0.92, 0.62 + 0.12 * margin)


def _pol_other(t: str, m: str) -> Tuple[int, float]:
    if re.search(r"\b(?:beats?|tops?|surpass\w*)\b", t) and re.search(r"\bmiss(?:es|ed)?\b", t):
        return 0, 0.6        # one line beats, another misses: mixed
    pol, _, _, margin = _direction(m)
    if pol == 0:
        return 0, 0.3
    return pol, min(0.75, 0.5 + 0.1 * margin)


# ── about the company? ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def _alias_forms(a: str) -> Tuple[str, ...]:
    a = _norm(a).replace("&", " and ")
    a = " ".join(a.split())
    if not a:
        return ()
    forms = [a]
    words = a.split()
    while len(words) > 1 and words[-1] in _GENERIC_ALIAS_WORDS:
        words = words[:-1]
        if len(" ".join(words)) >= 3:
            forms.append(" ".join(words))
    return tuple(dict.fromkeys(forms))


def _about(t: str, aliases: Iterable[str], etype: str) -> Tuple[bool, bool]:
    """-> (about_company, alias_known)."""
    if etype == "none":
        return False, bool(aliases)
    als = [x for x in (aliases or ()) if str(x or "").strip()]
    if not als:
        return True, False
    t2 = " " + t.replace("&", " and ") + " "
    for a in als:
        for f in _alias_forms(str(a)):
            if re.search(r"(?<![a-z0-9])" + re.escape(f) + r"(?![a-z0-9])", t2):
                return True, True
    return False, True


# ── classify ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
_LOCK = threading.Lock()
_STATS: Dict[str, int] = {"calls": 0, "none": 0, "unsure": 0, "errors": 0}


def stats() -> Dict[str, Any]:
    with _LOCK:
        return dict(_STATS)


def reset_stats() -> None:
    with _LOCK:
        for k in list(_STATS):
            _STATS[k] = 0


def _bump(k: str) -> None:
    with _LOCK:
        _STATS[k] = _STATS.get(k, 0) + 1


def _out(etype: str, pol: int, strength: float, about: bool, new: bool, conf: float, reason: str) -> Dict[str, Any]:
    return {"type": etype, "pol": int(pol), "strength": round(max(0.0, min(1.0, strength)), 2), "about_company": bool(about),
            "is_new_info": bool(new), "confidence": round(max(0.0, min(1.0, conf)), 2), "reason": reason}


def _classify(headline: str, aliases: Iterable[str]) -> Dict[str, Any]:
    t = _norm(headline)
    if not t:
        return _out("none", 0, 0.0, False, False, 0.0, "empty")
    m = _mask(t)
    new = not _NO_NEW_INFO.search(t)

    strong_result = bool(_RESULT_STRONG.search(t))
    q_term = bool(_RESULT_FORCE_DIV.search(t))
    if _NONE.search(t) and not (strong_result and q_term and not _NO_NEW_INFO.search(t) and not _NONE.search(_NONE_SOFT.sub(" ", t))):
        ab, _ = _about(t, aliases, "none")
        return _out("none", 0, 0.0, ab, False, 0.9, "market wrap / list / opinion")

    etype, pol, conf, why = "other", 0, 0.3, "no event cue"

    if _RUMOUR.search(t):
        etype, pol, conf, why = "other", 0, 0.8, "rumour / denial"
    elif _TALKS.search(t):
        etype, pol, conf, why = "other", 0, 0.7, "talks / unconfirmed"
    elif _INVESTOR_BOOK.search(t):
        etype, pol, conf, why = "other", 0, 0.7, "investor portfolio"
    elif _BOARD.search(t):
        etype, pol, conf, why = "board_meeting", 0, 0.85, "board meeting / AGM"
    elif _MGMT.search(t) and not _BULK.search(t):
        etype = "management_change"
        pol, conf = _pol_mgmt(t)
        why = "management change"
    elif _RATING.search(t):
        etype = "rating_change"
        pol, conf = _pol_rating(t, m)
        why = "analyst / agency rating"
    elif _USFDA_APPROVAL.search(t) and not re.search(r"\b(?:warning|483|import alert|observations?)\b", t):
        etype = "guidance"
        pol, conf = 1, 0.8
        why = "regulatory approval for a product"
    elif _REG.search(t) and not (_LEGAL.search(t) and re.search(r"\b(?:sebi|court|nclt|raid|ed|cbi|show[- ]cause)\b", t) and not re.search(r"\b(?:rbi|irdai|dgca|usfda|trai)\b|\bwarning letter\b|\bform 483\b|\brecalls?\b", t)):
        etype = "regulatory"
        pol, conf = _pol_reg(t, m)
        why = "regulator / inspection"
    elif _LEGAL.search(t):
        etype = "legal"
        pol, conf = _pol_legal(t, m)
        why = "legal / enforcement"
    elif _BULK.search(t):
        etype = "bulk_block"
        pol, conf = _pol_flow(t)
        why = "bulk / block deal"
    elif _INSIDER.search(t):
        etype = "insider"
        pol, conf = _pol_flow(t)
        why = "promoter / insider"
    elif _DIVIDEND.search(t) and not (strong_result and q_term):
        etype = "dividend"
        pol, conf = _pol_div(t, m)
        why = "dividend"
    elif _CORP.search(t) and not (strong_result and q_term):
        etype = "corporate_action"
        pol, conf = _pol_corp(t, m)
        why = "corporate action"
    elif _GUIDE_EXPLICIT.search(t) and not q_term:
        etype = "guidance"
        pol, conf = _pol_guidance(t, m)
        why = "guidance / outlook"
    elif strong_result:
        etype = "results"
        pol, conf = _pol_results(t, m)
        why = "results"
    elif _ACCIDENT.search(t):
        etype = "other"
        pol, conf = -1, 0.7
        why = "accident / disruption"
    elif _GUIDE.search(t):
        etype = "guidance"
        pol, conf = _pol_guidance(t, m)
        why = "order / project / launch"
    elif _DIVIDEND.search(t) or _CORP.search(t):
        etype = "dividend" if _DIVIDEND.search(t) else "corporate_action"
        pol, conf = (_pol_div(t, m) if etype == "dividend" else _pol_corp(t, m))
        why = "result day action"
    else:
        etype = "other"
        pol, conf = _pol_other(t, m)
        why = "share move / other" if _MOVE.search(t) else "no event cue"

    # a dividend / corporate action with a result term but no direction of its own: the result words decide
    ab, known = _about(t, aliases, etype)
    strength = _BASE_STRENGTH.get(etype, 0.3)
    if pol == 0:
        strength *= 0.5
    if re.search(r"\b\d{2,3}(?:\.\d+)?\s*%", t) and pol != 0:
        strength += 0.1
    if re.search(r"\b(?:record|plunge[sd]?|surge[sd]?|crash\w*|soar\w*|biggest|highest|lowest|worst)\b", t) and pol != 0:
        strength += 0.1
    if known and not ab:
        conf *= 0.5
        why += "; not about the named company"
    if not new:
        conf *= 0.8
    return _out(etype, pol, strength, ab, new, conf, why)


# Round 181: a trailing publisher tag such as " - NDTV Profit" is not part of the headline. The word "Profit" in it made
# "Steel Prices Rise: Tata Steel, JSW Steel, SAIL Raise Rates ... - NDTV Profit" a positive RESULTS event (0.86) on SAIL.
_PUBLISHER_TAIL = re.compile(
    r"\s+[-\u2013\u2014|]\s+(?:ndtv\s*profit|ndtv|moneycontrol(?:\.com)?|livemint|mint|business\s*standard|the\s*economic\s*times|economic\s*times|et\s*now|"
    r"et\s*markets|et\s*prime|times\s*of\s*india|the\s*hindu\s*businessline|hindu\s*businessline|business\s*line|financial\s*express|cnbc[\s-]*tv18|"
    r"cnbctv18|zee\s*business|business\s*today|reuters|bloomberg(?:\s*quint)?|bq\s*prime|news18|india\s*today|outlook\s*business|trendlyne|"
    r"marketsmojo|upstox|groww|angel\s*one|investing\.com(?:\s*india)?|the\s*print|theprint|firstpost|hindustan\s*times|yahoo\s*finance|"
    r"google\s*news|msn|tipranks|scanx|stockinsights|the\s*indian\s*express|indian\s*express|pti|ians)\s*$", re.I)


def _strip_publisher(headline: str) -> str:
    s = str(headline or "")
    for _ in range(2):
        t = _PUBLISHER_TAIL.sub("", s).rstrip()
        if t == s or not t:
            break
        s = t
    return s


def classify(headline: str, aliases: Iterable[str] = ()) -> Dict[str, Any]:
    """Classify one headline. Never raises. See the module docstring for the fields."""
    try:
        headline = _strip_publisher(headline)
        r = _classify(headline, aliases or ())
        _bump("calls")
        if r["type"] == "none":
            _bump("none")
        elif r["pol"] == 0 and r["confidence"] < _min_conf():
            _bump("unsure")
        return r
    except Exception:  # noqa: BLE001 - a classifier must never break the feed
        _bump("errors")
        return _out("none", 0, 0.0, False, False, 0.0, "error")


def sentiment(headline: str, aliases: Iterable[str] = ()) -> float:
    """Headline sentiment in [-0.8, +0.8] (the same scale as the old Hugging Face scorer: +-0.8 / 0.0). 0.0 means no opinion."""
    try:
        r = classify(headline, aliases)
        if r["type"] == "none" or r["pol"] == 0 or r["confidence"] < _min_conf():
            return 0.0
        mag = 0.4 + 0.4 * float(r["strength"])
        return round(max(-0.8, min(0.8, r["pol"] * mag)), 2)
    except Exception:  # noqa: BLE001
        return 0.0


def event_category(headline: str, aliases: Iterable[str] = ()) -> Optional[str]:
    """The event type for event/main.py, or None for 'other' / 'none' / unsure (those stay unclassified, as before)."""
    try:
        r = classify(headline, aliases)
        if r["type"] in ("other", "none"):
            return None
        return r["type"]
    except Exception:  # noqa: BLE001
        return None
