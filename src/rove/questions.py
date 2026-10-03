"""One code-owned answer path for application-form questions.

Every question gets a canonical identity, a class, and an answer from the first source
that is allowed to answer it:

1. the approved profile
2. an answer the owner gave before to the same canonical question
3. a policy default, for plain questions only
4. a Qwen draft, for plain questions only
5. the owner, asked once

Legal and personal questions (the sensitive class) stop after step 2: no default and no
model draft ever answers them. The rules here are exact on purpose. A legal rule matches
only when every word of the label is one the rule knows, so a negation, another country
or an added condition falls through to the owner instead of being answered for him.

This module reads no state: callers pass the frozen profile and a recall function.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from . import form_reading

PLAIN, SENSITIVE = "plain", "sensitive"
POSITIVE, NEGATED = "positive", "negated"

REMEMBERED = "your earlier answer"
DECLINED = "policy.decline_self_identification"
USED_DRAFT = "Qwen's draft, used under your policy"


@dataclass(frozen=True)
class Question:
    """What a form field asks, independent of how this form words it."""

    canonical_id: str  # a known meaning, or "q:" plus a fingerprint of the wording
    sensitivity: str  # plain | sensitive
    polarity: str  # positive | negated
    scope: str  # "", us, unspecified, job_location, other:<place>, employer
    known: bool = False
    topic: str = ""  # the sensitive class, when there is one
    default: str = ""  # the policy-default rule that may answer it, when there is one
    detail: str = ""  # rule-specific: sponsorship tense, salary unit
    name: str = ""  # the label as the rules read it
    variant: str = ""  # the wording, when the rule tolerates words that change the question
    answerable: bool = True  # False when the form gives code nothing to go on


def normalized(text) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def plain_name(label) -> str:
    """The label as a rule reads it; "(Optional)" and "(Required)" are not part of it."""
    return " ".join(re.sub(r"\b(optional|required)\b", " ", normalized(label)).split())


def wording(label) -> str:
    """The label as an unknown question is fingerprinted: every word, qualifiers aside."""
    return normalized(re.sub(r"\((optional|required)\)|\*", " ", str(label or "").lower()))


def _words(text: str) -> frozenset:
    return frozenset(text.split())


# --- sensitive classes -------------------------------------------------------------
# One pattern set decides what only the owner or his approved profile may answer. The
# patterns run on normalized words, so "race" never matches "embrace". A false positive
# costs one question to the owner; a false negative lets a default or a draft answer a
# legal question, so the patterns lean wide.
SENSITIVE_PATTERNS = tuple(
    (topic, re.compile(r"\b(?:" + pattern + r")\b"))
    for topic, pattern in (
        (
            "export_control",
            (
                r"export\w*|itar|ear|sanction\w*|embargo\w*|(?:restricted|denied) part(?:y|ies)|debarred"
                r"|(?:u s|us) persons?|foreign (?:nationals?|persons?)|iran|cuba|north korea|syria|crimea"
            ),
        ),
        ("sponsorship", r"sponsor\w*"),
        (
            "authorization",
            (
                r"authori[sz]\w*|right to work|eligib\w+ to work|eligib\w+ for employment"
                r"|employment eligibility|legally (?:work|able|permitted|entitled|eligible|allowed)"
                r"|lawfully|work permit|permitted to work"
                r"|(?:able|available|allowed|entitled|eligible|free) to (?:legally |lawfully )?work in "
                r"(?:the )?(?:united states|u s a|usa|u s|us|country)"
            ),
        ),
        (
            "visa",
            (
                r"visas?|h ?1 ?b|h1 b|green card|permanent residen\w*|immigration|(?:non ?)?immigrant"
                r"|opt(?! in| out| into)|cpt|ead|i 9|e verify|f ?1|j ?1"
            ),
        ),
        (
            "citizenship",
            r"citizens?|citizenship|nationalit(?:y|ies)|national origin|(?:country|place) of birth",
        ),
        ("clearance", r"clearances?|polygraph|top secret|ts sci|classified"),
        (
            "criminal",
            (
                r"convict\w*|criminal\w*|felon(?:y|ies)|misdemeanou?rs?|arrest\w*|crimes?|plead\w*|pled"
                r"|probation|parole|incarcerat\w*|offen[cs]es?"
            ),
        ),
        (
            "background_check",
            (
                r"background (?:checks?|screen\w*|investigation\w*|verification)"
                r"|drug (?:test\w*|screen\w*)|credit checks?"
            ),
        ),
        ("veteran", r"veterans?|military|armed forces"),
        ("disability", r"disab\w*|accommodations?"),
        (
            "demographics",
            (
                r"gender\w*|sex|race|racial|ethnic\w*|hispanic|latin[oaxe]|sexual orientation|lgbt\w*"
                r"|transgender|pronouns?|self identif\w*|marital status|married|religio\w*|pregnan\w*"
                r"|demographic\w*|eeo\w*|equal (?:employment )?opportunity"
            ),
        ),
        (
            "age",
            (
                r"date of birth|birth ?dates?|birthdays?|dob|years? of age|years old|age|aged"
                r"|(?:18|eighteen) or (?:older|over|above)|(?:over|under|at least) (?:18|eighteen)"
            ),
        ),
        (
            "identity_document",
            (
                r"social security|ssn|passports?|driver s licen[cs]e|bank account|routing number"
                r"|tax id\w*|national id\w*"
            ),
        ),
        (
            "salary",
            (
                r"salar(?:y|ies)|compensation|wages?|remuneration|stipend|hourly rate|rate of pay"
                r"|(?:desired|expected) pay|pay (?:rate|range|expectations?|requirements?)"
            ),
        ),
        (
            "agreement",
            (
                r"certify|certifies|i hereby|attest\w*|affirm\w*|swear|agree\w*|consent\w*|signature\w*"
                r"|e sign\w*|signed|to sign|sign (?:here|below)|acknowledg\w*|privacy"
                r"|terms (?:and conditions|conditions|of (?:use|service|employment))"
                r"|non ?compete|non ?disclosure|nda|arbitrat\w*|binding|truthful\w*|true and"
                r"|perjury|misrepresent\w*|falsif\w*|waive\w*|release of information"
            ),
        ),
    )
)

# Voluntary self-identification: the questions a form's own decline option may answer.
SELF_ID = re.compile(
    r"\b(?:gender\w*|sex|race|racial|ethnic\w*|hispanic|latin[oaxe]|veterans?|disab\w*"
    r"|sexual orientation|lgbt\w*|transgender|self identif\w*)\b"
)
DECLINE_OPTION = re.compile(
    r"decline|prefer not|don.?t wish|do not wish|don.?t want to|do not want to|"
    r"not to (answer|disclose|self)|choose not|rather not",
    re.IGNORECASE,
)

# A profile policy value that keeps a class with the owner on every form.
ASK_EACH_TIME = "ask_each_time"
POLICY_KEYS = {
    "export_control": ("eligibility", "export_control_questions"),
    "demographics": ("application_policy", "demographics"),
    "disability": ("application_policy", "disability"),
    "veteran": ("application_policy", "veteran_status"),
    "salary": ("application_policy", "salary_questions"),
}


def sensitive_topic(name: str) -> str:
    for topic, pattern in SENSITIVE_PATTERNS:
        if pattern.search(name):
            return topic
    return ""


# --- places -------------------------------------------------------------------------
US = r"(?:the )?(?:united states(?: of america)?|u s a|usa|u s|us)"
US_NAMED = re.compile(r"\b(?:united states(?: of america)?|u s a|usa|u s|us)\b")
# "us" is the country only where the words around it say so; otherwise it is the employer.
US_COUNTRY_CONTEXT = re.compile(
    r"\b(?:united states(?: of america)?|u s a|usa|u s)\b"
    r"|\b(?:in|within|inside|outside|outside of) (?:the )?us\b|\bthe us\b"
    r"|\bus (?:citizen\w*|persons?|nationals?|work|based|resident\w*|government|military|visa"
    r"|export|laws?|security|territor\w*|employment|veterans?)\b"
)
COUNTRY_ALIASES = {"united states", "united states of america", "usa", "us", "u s", "u s a"}

# Countries and regions other than the United States. A sponsorship, authorization or
# citizenship question that names one is a different question from the US one. Names that
# are also US states or common first names (Georgia, Jordan, Chad) are left out: a legal
# rule rejects any word it does not know anyway.
OTHER_PLACES = (
    "afghanistan|albania|algeria|andorra|angola|argentina|armenia|australia|austria|azerbaijan"
    "|bahamas|bahrain|bangladesh|barbados|belarus|belgium|belize|benin|bhutan|bolivia|bosnia"
    "|botswana|brazil|brunei|bulgaria|burkina faso|burundi|cambodia|cameroon|canada|chile|china"
    "|colombia|costa rica|croatia|cyprus|czech republic|czechia|denmark|dominican republic"
    "|ecuador|egypt|el salvador|estonia|ethiopia|fiji|finland|france|gabon|gambia|germany|ghana"
    "|greece|guatemala|guinea|guyana|haiti|honduras|hong kong|hungary|iceland|india|indonesia"
    "|iraq|ireland|israel|italy|ivory coast|jamaica|japan|kazakhstan|kenya|kuwait|kyrgyzstan"
    "|laos|latvia|lebanon|liberia|libya|liechtenstein|lithuania|luxembourg|madagascar|malawi"
    "|malaysia|maldives|mali|malta|mauritius|mexico|moldova|monaco|mongolia|montenegro|morocco"
    "|mozambique|myanmar|namibia|nepal|netherlands|new zealand|nicaragua|niger|nigeria"
    "|north macedonia|norway|oman|pakistan|panama|paraguay|peru|philippines|poland|portugal"
    "|puerto rico|qatar|romania|russia|rwanda|saudi arabia|senegal|serbia|singapore|slovakia"
    "|slovenia|somalia|south africa|south korea|korea|spain|sri lanka|sudan|sweden|switzerland"
    "|taiwan|tajikistan|tanzania|thailand|tunisia|turkey|turkiye|uganda|ukraine"
    "|united arab emirates|uae|united kingdom|uk|great britain|britain|england|scotland|wales"
    "|northern ireland|uruguay|uzbekistan|venezuela|vietnam|yemen|zambia|zimbabwe"
    "|iran|cuba|north korea|syria|crimea"
    "|europe|european union|eu|eea|emea|apac|asia|africa|latin america|latam|south america"
    "|north america|middle east|oceania"
    "|another country|other countr(?:y|ies)|any other country|(?:your |my )?home country"
    "|(?:your |my )?country of (?:residence|origin|citizenship)|abroad|overseas|internationally"
)
OTHER_PLACE = re.compile(r"\b(?:" + OTHER_PLACES + r")\b")
JOB_PLACE = re.compile(
    r"\b(?:(?:the|this) (?:country|location|region|jurisdiction)"
    r"(?: (?:in which|where|that) (?:this|the) (?:job|role|position|opportunity|internship) "
    r"is (?:located|based)| (?:in which|where) (?:you are|i am) applying(?: to work)?"
    r"| of employment)?"
    r"|(?:the|your) country of employment"
    r"|the location (?:of|for) this (?:job|role|position))\b"
)
WHERE = rf"(?:{US}|xplace|xjobplace)"


def with_places(name: str) -> tuple[str, list[str]]:
    """The name with non-US places replaced by a placeholder, and the places found."""
    text = name.replace("new mexico", "newmexico")
    found = [m.group(0) for m in OTHER_PLACE.finditer(text)]
    text = OTHER_PLACE.sub("xplace", text)
    text = JOB_PLACE.sub("xjobplace", text)
    return text, found


def place_scope(text: str, found: list[str]) -> str:
    if "xplace" in text.split():
        return "other:" + "+".join(sorted(set(found)))
    if "xjobplace" in text.split():
        return "job_location"
    return "us" if US_NAMED.search(text) else "unspecified"


# --- negation -----------------------------------------------------------------------
NEGATION_WORDS = _words("not no never without neither nor none cannot outside except unless")
ANTONYMS = {
    "unwilling": "willing",
    "unable": "able",
    "unavailable": "available",
    "uncomfortable": "comfortable",
    "ineligible": "eligible",
    "unauthorized": "authorized",
    "unauthorised": "authorised",
}
CONTRACTION = re.compile(
    r"\b(are|is|do|does|did|will|would|can|could|have|has|had|should|were|was)n t\b"
)


def affirmative(name: str) -> tuple[str, bool]:
    """The name with its negations removed, and whether there were any."""
    text = re.sub(r"\byes (?:or )?no\b|\by n\b", " ", name)
    text = CONTRACTION.sub(r"\1 not", text)
    text = re.sub(r"\bwon t\b", "will not", text)
    text = re.sub(r"\bcan t\b", "can not", text)
    words, negated = [], False
    for word in text.split():
        if word in NEGATION_WORDS:
            negated = True
        elif word in ANTONYMS:
            negated = True
            words.append(ANTONYMS[word])
        else:
            words.append(word)
    return " ".join(words), negated


# --- canonical rules ----------------------------------------------------------------
IDENTITY_NAMES = {
    "first_name": "first name|legal first name|first name legal|given name|legal given name",
    "middle_name": "middle name|legal middle name",
    "last_name": "last name|legal last name|last name legal|family name|surname",
    "preferred_name": "preferred name|preferred first name",
    "full_name": "name|full name|legal name|legal full name|full legal name|your name"
    "|your full name",
    "email": "email|email address|e mail|e mail address",
    "phone": "phone|phone number|mobile phone|mobile number|cell phone|mobile phone number"
    "|cell phone number|telephone|telephone number|phone number including country code"
    "|phone no|mobile no",
    "phone_type": "contact phone type|phone type|phone number type",
    "location_city": "city|location city|current city",
    "state_region": "state|state region|state province|state province region",
    "country": "country|country region|country region of residence|country of residence",
    "postal_code": "zip code|postal code|zip|zip postal code",
    "location": "location|current location|city state|city and state|where are you located"
    "|where are you currently located|where are you based",
    "linkedin_url": "linkedin|linkedin profile|linkedin url|linkedin profile url",
    "github_url": "github|github url|github profile",
    "portfolio_url": "portfolio|website|personal website|portfolio url",
    "school": "school|university|college university",
    "major": "major|field of study|what is your field of study",
    "degree": "degree",
    "gpa": "gpa|current gpa",
    "sex": "sex",
    "sexual_orientation": "sexual orientation|what is your sexual orientation",
    "citizenship_country": "citizenship|country of citizenship|what is your country of citizenship"
    "|nationality|what is your nationality",
    "how_did_you_hear": "source|referral source|application source|how did you find us",
}
# Common questions with one meaning however a form words them, none an identity fact.
# The warm-up in `#memory` (common_questions.py) asks each once; the answer then fills
# every wording here. Bare "end date" or "languages" are left out on purpose: in a work
# history or on a developer form they mean something else.
COMMON_NAMES = {
    "street_address": "address|street address|address line 1|street address line 1|home address"
    "|mailing address|current address|residential address|address 1",
    "languages_spoken": "languages spoken|spoken languages|what languages do you speak"
    "|which languages do you speak|languages you speak|what languages are you fluent in"
    "|which languages are you fluent in|fluent languages|languages you are fluent in",
    "work_style_preference": "preferred work arrangement|work arrangement preference"
    "|what is your preferred work arrangement|preferred work style|work style preference"
    "|preferred work setting|which work setting do you prefer|preferred work environment"
    "|work location preference|preferred work location type|work mode|preferred work mode"
    "|preferred working arrangement|remote hybrid or on site|remote hybrid or onsite"
    "|do you prefer remote hybrid or on site|do you prefer remote hybrid or onsite"
    "|are you looking for remote hybrid or on site work|remote or on site|remote or onsite",
    "preferred_locations": "preferred location|preferred locations|preferred work location"
    "|preferred work locations|preferred office location|preferred office locations"
    "|preferred office|which office location do you prefer|location preference"
    "|location preferences|what is your preferred location|what are your preferred locations"
    "|where would you like to work|preferred city|preferred cities",
}
EXACT_NAMES = {
    name: key
    for key, names in (*IDENTITY_NAMES.items(), *COMMON_NAMES.items())
    for name in names.split("|")
}
PLACE_LABELS = frozenset(
    {"location", "current location", "location city", "city", "city state", "where are you located"}
)

# A label that contains a fact's name means that fact only when every other word is a
# neutral qualifier: "LinkedIn Profile URL" is LinkedIn, "Referrer email" is nobody's.
NEEDLES = (
    ("linkedin", "linkedin_url"),
    ("github", "github_url"),
    ("portfolio", "portfolio_url"),
    ("personal website", "portfolio_url"),
    ("personal site", "portfolio_url"),
    ("website", "portfolio_url"),
    ("profile link", "portfolio_url"),
    ("profile url", "portfolio_url"),
    ("online profile", "portfolio_url"),
    ("given name", "first_name"),
    ("first name", "first_name"),
    ("family name", "last_name"),
    ("surname", "last_name"),
    ("last name", "last_name"),
    ("e mail", "email"),
    ("email", "email"),
    ("mobile", "phone"),
    ("phone", "phone"),
    ("zip", "postal_code"),
    ("postal", "postal_code"),
)
QUALIFIERS = _words(
    "your my the a an please enter provide add share current personal primary best contact"
    " number address url link profile account page of for to legal code zip postal if any"
    " applicable available you have one what is confirm again re candidate applicant s"
)

AUTHORIZED = re.compile(
    r"(?:(?:are|will) you (?:be )?(?:(?:currently|presently|legally|lawfully) )*"
    r"(?:authori[sz]ed|eligible|permitted|allowed|entitled|(?:legally|lawfully) able)"
    r"|do you (?:currently |presently )?have (?:the )?(?:(?:legal|lawful) )?"
    r"(?:right|authori[sz]ation|permission|eligibility)"
    r"|(?:legally |lawfully )?authori[sz]ed)"
    r" to (?:(?:legally|lawfully) )?(?:work|be employed)(?: (?:legally|lawfully))?"
    rf"(?: in (?P<where>{WHERE}))?(?P<tail>(?: [a-z0-9]+)*)"
)
AUTHORIZED_TAIL = _words(
    "for any an a the this our employer company organization role position job on full time"
    " basis at currently now legally lawfully"
)
WITHOUT_SPONSORSHIP = re.compile(
    r" without (?:the need for |needing |requiring |any (?:need for )?)?"
    r"(?:(?:visa|employer|company|immigration|employment|work) )*sponsorship"
    r"(?: now or in the future)?"
)
SPONSORSHIP_WORDS = _words(
    "will do would you now or in the future currently presently ever either at any time point"
    " require need an a our company employer employment based visa visas immigration work"
    " working authorization permit h1b h1 h 1b 1 b status sponsorship for to of e g i such as"
    " example like legally lawfully continue be employed remain stay order obtain maintain"
    " extend extension type kind form related support opt cpt stem f1 f j1 j tn l1 l o1 o e3"
    " lawful legal from this your us u s usa united states america xplace xjobplace"
)
CITIZEN = re.compile(
    rf"(?:(?:are you|i am) (?:currently )?an? )?(?:(?:{US}|xplace) citizen|citizen of (?:{US}|xplace))"
)
ADULT = re.compile(
    r"are you (?:(?:at least |over |above )?(?:18|eighteen)(?: years)?(?: of age| old)?"
    r"(?: or (?:older|over|above))?|(?:over|above|at least) the age of (?:18|eighteen))"
)
CLEARANCE = re.compile(
    r"do you (?:currently |presently )?(?:have|hold|possess) (?:an? |any )?"
    rf"(?:(?:active|current|valid) )*(?:{US} )?(?:government )?security clearance"
)
FELONY = re.compile(r"have you ever been convicted of a felony")
CRIME = re.compile(r"have you ever been convicted of (?:a|any) (?:crime|criminal offen[cs]e)")

# Vocabulary rules: (canonical id, words one of which must appear, every allowed word).
VOCABULARY_RULES = (
    (
        "gender",
        "gender",
        (
            "gender identity what is your my please select choose indicate i identify as do you"
            " with which how would describe voluntary self identification the of to"
        ),
    ),
    (
        "hispanic_latino",
        "hispanic latino latina latinx",
        "are you hispanic latino latina latinx or of origin ethnicity do identify as a i am",
    ),
    (
        "veteran_status",
        "veteran",
        (
            "veteran status protected are you a an what is your my please select identify i as do"
            " us u s military of the armed forces self identification voluntary indicate"
        ),
    ),
    (
        "disability_status",
        "disability disabilities",
        (
            "disability disabilities status do you have a an or had ever history record of one"
            " please select indicate i identify as having voluntary self identification what is"
            " your my with person are individual in the past currently previously"
        ),
    ),
    (
        "pronouns",
        "pronouns pronoun",
        "pronouns pronoun preferred your my what are personal please select share gender",
    ),
)
# Race and ethnicity are two questions on many forms and one on others: three ids.
RACE_WORDS = _words(
    "race ethnicity ethnic racial background origin group category what is your my please"
    " select identify indicate i as do you how would describe and or the which best"
    " describes voluntary self identification"
)
TRUTHFUL_WORDS = _words(
    "i hereby certify attest affirm confirm declare acknowledge that the all information"
    " answers statements provided given submitted in on this my application form resume is are"
    " true complete correct accurate truthful and to best of knowledge above herein by"
    " checking box submitting signing below have has been understand any false misleading"
    " misrepresentation omission falsification may be grounds for result rejection dismissal"
    " termination employment discharge refusal hire withdrawal offer disqualification or if"
    " employed cause immediate lead"
)
PRIVACY_WORDS = _words(
    "i have read and agree to the our privacy policy notice statement consent acknowledge"
    " accept understand understood reviewed by checking this box that processing of my"
    " personal data information in accordance with as described do you"
)
SALARY_WORDS = _words(
    "salary compensation pay expectation expectations desired expected what is are your annual"
    " yearly base range requirement requirements for this role position in usd please provide"
    " hourly rate per hour year target"
)
CONTACT_NOUNS = _words(
    "contact contacted text texts sms message messages call calls email emails communications"
)
CONTACT_VERBS = _words("agree consent opt may can allow like ok okay willing happy")
CONTACT_WORDS = _words(
    "i do you agree consent to be being contacted contact receive receiving by via text texts"
    " sms message messages messaging phone call calls email emails e mail from the company"
    " recruiter recruiters recruiting team hiring our us about regarding this my your"
    " application job opportunity opportunities future openings roles positions status updates"
    " interview scheduling and or can may we me at number provided above would like is it ok"
    " okay opt in for communications related employment allow yes please send reach out mobile"
    " data rates apply talent network community pool join joining added add keep kept"
    " considered consider other notified notify are willing happy"
)
MARKETING_WORDS = CONTACT_WORDS | _words(
    "marketing newsletter newsletters promotional promotions news offers product products"
    " subscribe events"
)

HEARD = re.compile(
    r"(?:how|where) did you (?:first )?(?:(?:hear|learn|find out) (?:about|of)"
    r"|find(?= (?:us|this|our)\b)|come across)(?: [a-z0-9]+){1,8}"
)
READ_POSTING = re.compile(
    r"(?:have you|i have|i acknowledge that i have|i confirm that i have) read"
    r"(?: and (?:understood|understand))? the (?:(?:full|entire|complete) )?"
    r"(?:job|role|position) (?:description|posting|requirements|duties)"
    r"(?: and (?:requirements|responsibilities|qualifications))?"
    r"(?: for this (?:job|role|position)| above| in full| carefully)?"
)
START_DATE = re.compile(
    r"(?:when|what date) (?:can|could|would) you (?:be able to )?(?:start|begin)(?: work(?:ing)?)?"
    r"|when are you available to (?:start|begin)(?: work(?:ing)?)?"
    r"|(?:what is your )?(?:(?:earliest|available|desired|preferred|anticipated) )*"
    r"(?:possible )?start date|date available(?: to start)?|available start date"
)
GRADUATION_DATE = frozenset(
    {
        "when is your expected graduation date month year",
        "expected graduation date",
        "anticipated graduation date",
        "graduation date",
        "expected graduation",
        "what is your expected graduation date",
        "what is your graduation date",
        "when do you expect to graduate",
        "when will you graduate",
    }
)
GRADUATION_YEAR = frozenset({"graduation year", "expected graduation year", "year of graduation"})
LOCATED_IN_US = re.compile(
    rf"are you (?:currently )?(?:located|based|residing|living)(?: and (?:located|based))? in {US}"
)
RELOCATION_HELP = re.compile(
    r"(?:will|do|would) you (?:require|need) relocation (?:assistance|support)"
    r"(?: for this (?:job|role|position))?"
)
WORKED_HERE = re.compile(
    r"have you (?:(?:ever|previously) )*(?:worked|been employed|been an employee|interned)"
    r"(?: previously| before| in the past)? (?:for|at|by|with)(?: [a-z0-9]+){1,6}"
)
APPLIED_HERE = re.compile(
    r"have you (?:(?:ever|previously) )*applied(?: previously| before| in the past)?"
    r" (?:to|at|with|for)(?: [a-z0-9]+){1,6}"
)
WILLING = (
    r"(?:(?:are|would|will) you (?:be )?|i am )(?:(?:currently|also) )?"
    r"(?:willing|able|open|prepared|comfortable|okay|ok|happy|available)"
    r"(?: and (?:willing|able))?"
)
WILLINGNESS = re.compile(
    rf"(?:{WILLING} (?:to|with|for|working|relocating|commuting)|"
    r"can you (?:work|commute|come|be|start|relocate|travel|attend)|"
    r"willing(?:ness)? to (?:relocate|work|commute|travel)|open to relocation)\b"
)
RELOCATE = re.compile(r"\brelocat\w*\b")
ONSITE = re.compile(r"\b(?:on ?site|in person|hybrid|office|offices)\b")
STACK = re.compile(rf"{WILLING} (?:working |coding |programming |developing )?(?:with|in|using)\b")
# Only a stated willingness takes the general default; "are you able to" claims an ability.
STATED_WILLINGNESS = re.compile(
    r"(?:(?:are|would|will) you (?:be )?|i am )(?:(?:currently|also) )?"
    r"(?:willing|comfortable|open|okay|ok|happy|prepared)\b"
)
# Words that turn a willingness question into something "yes" does not safely answer.
RISK_WORDS = _words(
    "assistance expense expenses cost costs unpaid free volunteer equity commission deferred fee"
    " fees deposit bond purchase invest pay paid cut own lower less reduced reduction restrict"
    " restriction restrictions restricted prevent prevents conflict conflicts limitation"
    " limitations immediately immediate asap within today tomorrow xplace"
)
EMPLOYER_RELATIVE = re.compile(
    r"\b(?:here|us|our|this (?:company|organization|firm|employer|role|position|job|opportunity"
    r"|internship|team|program)|the (?:company|role|position))\b"
)
# More common questions (see COMMON_NAMES), matched whole. Each names what it asks:
# a bare "end date" or "references" field is never taken for one of these.
HOURS_PER_WEEK = re.compile(
    r"(?:(?:how many |what )?hours (?:per|a|each) week(?: (?:of )?availab\w*)?"
    r"(?: (?:are|can|could|would|will) you"
    r" (?:be )?(?:available|work|working|commit|dedicate)(?: to (?:work|working))?)?"
    r"|(?:how many|what) hours (?:are|can|could|would|will) you (?:be )?"
    r"(?:available|work|working|commit)(?: to work)? (?:per|a|each) week"
    r"|(?:desired|preferred|expected|available|weekly) hours(?: (?:per|a|each) week)?"
    r"|availability (?:in )?hours (?:per|a|each) week|hours of availability (?:per|a|each) week"
    r"|number of hours (?:per|a|each) week(?: (?:you are |you re )?available)?)"
    r"(?: (?:during|in|for|over) the (?:summer|semester|term|school year|internship))?"
)
AVAILABILITY_END = re.compile(
    r"(?:(?:your |my )?availability end date|end (?:date )?of (?:your |my )?availability"
    r"|when does your availability end"
    r"|(?:what is )?(?:the )?(?:last|latest) (?:day|date) (?:you are|you will be|you can be"
    r"|of your) availab\w*"
    r"|(?:until|through) (?:when|what date) (?:are|will) you (?:be )?available"
    r"|(?:what date )?are you available (?:until|through)|available (?:until|through)(?: date)?"
    r"|how long (?:are|will) you (?:be )?available(?: for)?"
    r"|(?:what is your )?(?:(?:preferred|desired|latest|internship|program|term|co ?op) )+end date"
    r"|end of (?:internship|term|program) date|(?:internship|term|program) end"
    r"|(?:what is your )?(?:latest|last) possible end date)"
)
REFERENCES = re.compile(
    r"(?:(?:are |do you have )?(?:professional |work )?references available"
    r"(?: (?:upon|on) request)?"
    r"|(?:can|could|are you able to|are you willing to|will you be able to|would you be able to)"
    r" (?:you )?provide (?:professional |work |us with )*references(?: (?:upon|on|if) request(?:ed)?)?"
    r"|do you have (?:professional |work )?references(?: (?:we|i) (?:can|may|could) contact)?)"
)
BACKGROUND_CHECK = re.compile(
    r"(?:(?:do you |would you |i )?(?:consent|agree|are you willing|are willing|am willing"
    r"|willing)(?: to)? (?:undergo |submit to |complete |participate in |authorize |take"
    r" |a |an |the )*(?:pre employment |pre hire |criminal |standard |routine )*"
    r"background (?:check|checks|screening|screen|investigation)"
    r"(?: (?:and|or|and or) (?:a )?drug (?:test|screen|screening))?"
    r"(?: (?:if|when|as|upon) (?:required|hired|offered employment|a condition of employment"
    r"|offer))?"
    r"|background check (?:consent|authorization|authorisation|acknowledgement|acknowledgment)"
    r"|consent to (?:a )?background check)"
)
ESSENTIAL_FUNCTIONS = re.compile(
    r"(?:are you able to|can you|are you capable of|would you be able to) perform(?:ing)? "
    r"(?:all |each of )?(?:the |all the )?essential (?:functions|duties|job functions"
    r"|responsibilities)(?: of (?:this|the) (?:job|position|role)(?: (?:for which you are"
    r" applying|you are applying for))?)?"
    r"(?: (?:with or without|with|without) (?:a |any )?(?:reasonable )?accommodations?)?"
)
DRIVERS_LICENSE = re.compile(
    r"do you (?:currently )?(?:have|hold|possess) (?:a |an )?(?:valid |current |active"
    r" |unrestricted )*(?:driver s|drivers|driving|driver) licen[cs]e"
    r"(?: and (?:reliable |your own |access to a )?(?:transportation|vehicle|car))?"
)

# The owner's rule for plain questions: yes across the board for willingness and
# acknowledgement questions that are not legal. Each row names a canonical id (or the
# general willingness rule) and the answer; a sensitive or negated question never reaches
# this table.
DEFAULTS = {
    "onsite_willing": "Yes",
    "commute_willing": "Yes",
    "travel_willing": "Yes",
    "available_for_term": "Yes",
    "comfortable_with_stack": "Yes",
    "willingness": "Yes",
    "contact_consent": "Yes",
    "talent_network_opt_in": "Yes",
    "read_job_description": "Yes",
}
# Trivial preference questions: "choose whatever is reasonable".
PREFERENCES = frozenset({"how_did_you_hear"})
# For a dropdown, the first tier with exactly one matching option wins.
HEARD_TIERS = (
    re.compile(r"job ?board|job (?:site|aggregator|search)|online job", re.IGNORECASE),
    re.compile(r"^other\b", re.IGNORECASE),
    re.compile(r"internet|online|web ?site|web search", re.IGNORECASE),
)
HEARD_TEXT = "Online job board"


def _only(words: list[str], need: frozenset | set, allowed: frozenset | set) -> bool:
    return bool(need & set(words)) and set(words) <= allowed


def _strict(name: str, text: str, found: list[str]) -> dict | None:
    """Rules that match a whole label word for word. `text` has place placeholders in."""
    words = text.split()
    if name in EXACT_NAMES:
        canonical = EXACT_NAMES[name]
        return {"id": canonical, "default": canonical if canonical in PREFERENCES else ""}
    match = AUTHORIZED.fullmatch(text)
    if match:
        tail = match.group("tail")
        scope = place_scope(match.group("where") or "", found)
        if WITHOUT_SPONSORSHIP.fullmatch(tail):
            return {"id": "work_authorization_without_sponsorship", "scope": scope}
        if set(tail.split()) <= AUTHORIZED_TAIL:
            return {"id": "work_authorization", "scope": scope}
    if (
        re.match(r"(?:will|do|would) you\b", text)
        and set(words) <= SPONSORSHIP_WORDS
        and "sponsorship" in words
        and any(v in words[: words.index("sponsorship")] for v in ("require", "need"))
    ):
        now = bool({"now", "currently", "presently", "ever"} & set(words))
        future = bool({"future", "ever"} & set(words))
        tense = "now_or_future" if now == future else ("now" if now else "future")
        return {"id": "sponsorship", "scope": place_scope(text, found), "detail": tense}
    if CITIZEN.fullmatch(text):
        return {"id": "citizenship", "scope": place_scope(text, found)}
    if ADULT.fullmatch(text):
        return {"id": "at_least_18"}
    if CLEARANCE.fullmatch(text):
        return {"id": "security_clearance"}
    if FELONY.fullmatch(text):
        return {"id": "criminal_history_felony"}
    if CRIME.fullmatch(text):
        return {"id": "criminal_history"}
    for canonical, need, allowed in VOCABULARY_RULES:
        if _only(words, set(need.split()), set(allowed.split())):
            return {"id": canonical}
    race, ethnicity = {"race", "racial"} & set(words), {"ethnicity", "ethnic"} & set(words)
    if (race or ethnicity) and set(words) <= RACE_WORDS:
        return {"id": "race_ethnicity" if race and ethnicity else "race" if race else "ethnicity"}
    if _only(words, {"certify", "attest", "affirm", "declare", "confirm"}, TRUTHFUL_WORDS) and {
        "true",
        "accurate",
        "correct",
        "truthful",
    } & set(words):
        return {"id": "certify_truthful"}
    if (
        _only(words, {"privacy"}, PRIVACY_WORDS)
        and {"policy", "notice", "statement"} & set(words)
        and {"agree", "consent", "acknowledge", "accept", "read", "reviewed"} & set(words)
    ):
        return {"id": "privacy_policy_consent"}
    if _only(words, {"salary", "compensation", "pay", "rate"}, SALARY_WORDS):
        hourly, annual = {"hourly", "hour"} & set(words), {"annual", "yearly", "year"} & set(words)
        unit = "_hourly" if hourly else "_annual" if annual else ""
        return {"id": "salary_expectation" + unit}
    # Acknowledgements the owner's rule covers. Their wording is checked word for word,
    # so they are plain even though "agree" and "consent" are sensitive words elsewhere.
    if _only(words, CONTACT_NOUNS, CONTACT_WORDS) and CONTACT_VERBS & set(words):
        return {"id": "contact_consent", "sensitivity": PLAIN, "default": "contact_consent"}
    if _only(words, {"talent"}, CONTACT_WORDS) and {"network", "community", "pool"} & set(words):
        return {
            "id": "talent_network_opt_in",
            "sensitivity": PLAIN,
            "default": "talent_network_opt_in",
        }
    if _only(words, {"marketing", "newsletter", "newsletters", "promotional"}, MARKETING_WORDS):
        return {"id": "marketing_opt_in", "sensitivity": PLAIN}
    if READ_POSTING.fullmatch(text):
        return {
            "id": "read_job_description",
            "sensitivity": PLAIN,
            "default": "read_job_description",
        }
    if START_DATE.fullmatch(text):
        return {"id": "start_availability"}
    if text in GRADUATION_DATE or text in GRADUATION_YEAR:
        return {"id": "graduation_date"}
    if LOCATED_IN_US.fullmatch(text):
        return {"id": "located_in_us", "scope": "us"}
    if RELOCATION_HELP.fullmatch(text):
        return {"id": "relocation_assistance_needed"}
    for canonical, pattern in COMMON_RULES:
        if pattern.fullmatch(text):
            return {"id": canonical}
    return None


COMMON_RULES = (
    ("hours_per_week", HOURS_PER_WEEK),
    ("availability_end", AVAILABILITY_END),
    ("references_available", REFERENCES),
    ("background_check_consent", BACKGROUND_CHECK),
    ("essential_functions", ESSENTIAL_FUNCTIONS),
    ("drivers_license", DRIVERS_LICENSE),
)


def _loose(name: str, text: str) -> dict | None:
    """Rules that read the start of a label and tolerate what follows.

    They cover plain questions only: a label that carries any sensitive word is never
    matched here, so a profile fact or a default cannot answer a legal question that
    happens to open like a willingness one.
    """
    if sensitive_topic(name):
        return None
    words = text.split()
    if HEARD.fullmatch(text):
        return {"id": "how_did_you_hear", "default": "how_did_you_hear"}
    if WORKED_HERE.fullmatch(text):
        return {"id": "previously_employed_here", "scope": "employer"}
    if APPLIED_HERE.fullmatch(text):
        return {"id": "previously_applied_here", "scope": "employer"}
    if not WILLINGNESS.match(text):
        # Any other question about graduating: answered only by an option that states
        # the approved graduation month.
        return {"id": "graduation_date"} if any(w.startswith("graduat") for w in words) else None
    # A willingness question: which one decides the profile fact or the default.
    risky = "risky" if RISK_WORDS & set(words) else ""
    if RELOCATE.search(text):
        return {"id": "relocate_willing", "detail": risky}
    if any(w.startswith("commut") for w in words):
        canonical = "commute_willing"
    elif ONSITE.search(text):
        canonical = "onsite_willing"
    elif {"travel", "traveling", "travelling"} & set(words):
        canonical = "travel_willing"
    elif STACK.match(text):
        canonical = "comfortable_with_stack"
    elif {"available", "start"} & set(words):
        canonical = "available_for_term"
    elif STATED_WILLINGNESS.match(text) and not US_NAMED.search(text):
        return {"default": "" if risky else "willingness"}
    else:
        return None
    return {"id": canonical, "default": "" if risky else canonical, "detail": risky}


def _needle(name: str) -> dict | None:
    """An identity fact named inside a short label whose other words are all neutral."""
    if len(name.split()) > 6:
        return None
    for needle, canonical in NEEDLES:
        if re.search(rf"\b{needle}\b", name):
            rest = re.sub(rf"\b{needle}\b", " ", name).split()
            return {"id": canonical} if set(rest) <= QUALIFIERS else None
    return None


def _known(name: str, loose: bool) -> dict | None:
    text, found = with_places(name)
    rule = _strict(name, text, found)
    if rule or not loose:
        return rule
    rule = _loose(name, text)
    if rule is None:
        return _needle(name)
    # A loose rule reads past a city, a schedule or a company name. Those words change
    # what an answer means, so the owner's answer is remembered for this wording only.
    return rule if rule.get("id") == "how_did_you_hear" else {**rule, "tolerant": True}


def classify(label, kind: str = "", options=()) -> Question:
    """Canonical identity, class, polarity and scope of one form question."""
    name = plain_name(label)
    bare, negated = affirmative(name)
    # A strict rule may contain its own "without"; everything else sees a negated label
    # only with the negation taken out and marked as the opposite question.
    rule, polarity = _known(name, loose=not negated), POSITIVE
    if rule is None and negated:
        # "Are you unwilling to relocate?" is the relocation question asked the other way
        # round: the same identity, never the same answer.
        rule, polarity = _known(bare, loose=True), NEGATED
    rule = rule or {}
    topic = sensitive_topic(name)
    canonical, scope = rule.get("id", ""), rule.get("scope", "")
    if canonical in {"work_authorization", "sponsorship", "citizenship"} and scope == "us":
        canonical += "_us"
    if rule.get("id") == "sponsorship":
        canonical += "_" + rule["detail"]
    if canonical == "work_authorization_without_sponsorship" and scope == "us":
        canonical = "work_authorization_us_without_sponsorship"
    fingerprint = hashlib.sha256(wording(label).encode()).hexdigest()[:24]
    worded = bool(canonical) and (bool(rule.get("tolerant")) or polarity == NEGATED)
    if not canonical:
        # Export-control wording varies too much for a rule: one id for the class, and
        # each wording stays its own question.
        canonical = "export_control" if topic == "export_control" else "q:" + fingerprint
        worded = topic == "export_control"
        text, found = with_places(name)
        if "xplace" in text.split() and topic:
            scope = "other:" + "+".join(sorted(set(found)))
        elif EMPLOYER_RELATIVE.search(US_COUNTRY_CONTEXT.sub(" ", name)):
            # "Why do you want to work here?" has a different answer at every employer.
            scope = "employer"
    sensitivity = rule.get("sensitivity") or (SENSITIVE if topic else PLAIN)
    if canonical in SENSITIVE_IDS:
        sensitivity = SENSITIVE
    return Question(
        canonical_id=canonical,
        sensitivity=sensitivity,
        polarity=polarity,
        scope=scope,
        known=bool(rule.get("id")),
        topic=topic or TOPIC_OF.get(canonical, ""),
        default=rule.get("default", "") if sensitivity == PLAIN and polarity == POSITIVE else "",
        detail=rule.get("detail", ""),
        name=name,
        variant=fingerprint if worded else "",
        # Nothing to go on: no text, an input's name instead of a question, or the
        # placeholder form reading gives a question it could not read.
        answerable=not form_reading.unreadable({"label": label})
        and bool(name)
        and kind not in {"password", "file", "hidden"},
    )


# Canonical ids that are sensitive whatever words the label happens to use.
TOPIC_OF = {
    "work_authorization": "authorization",
    "work_authorization_us": "authorization",
    "work_authorization_without_sponsorship": "authorization",
    "work_authorization_us_without_sponsorship": "authorization",
    "citizenship": "citizenship",
    "citizenship_us": "citizenship",
    "citizenship_country": "citizenship",
    "at_least_18": "age",
    "security_clearance": "clearance",
    "criminal_history": "criminal",
    "criminal_history_felony": "criminal",
    "gender": "demographics",
    "sex": "demographics",
    "sexual_orientation": "demographics",
    "hispanic_latino": "demographics",
    "race_ethnicity": "demographics",
    "race": "demographics",
    "ethnicity": "demographics",
    "pronouns": "demographics",
    "veteran_status": "veteran",
    "disability_status": "disability",
    "certify_truthful": "agreement",
    "privacy_policy_consent": "agreement",
    "salary_expectation": "salary",
    "salary_expectation_hourly": "salary",
    "salary_expectation_annual": "salary",
    "background_check_consent": "background_check",
    "essential_functions": "disability",
    "drivers_license": "identity_document",
}
SENSITIVE_IDS = frozenset(TOPIC_OF) | {
    f"sponsorship{place}_{tense}"
    for place in ("", "_us")
    for tense in ("now", "future", "now_or_future")
}


def is_sensitive(label, kind: str = "", options=()) -> bool:
    return classify(label, kind, options).sensitivity == SENSITIVE


def is_place_label(label) -> bool:
    return plain_name(label) in PLACE_LABELS


# For a question whose answer depends on the employer. An answer given outside any
# application (in `#memory`) holds for every employer; one given for an application whose
# employer cannot be told is neither kept nor reused.
ANY_EMPLOYER = "*"
NO_EMPLOYER = "?"


def memory_key(question: Question, employer: str = "") -> str | None:
    """What an owner's answer is remembered under: canonical id, polarity and scope.

    A question whose answer depends on the employer is remembered per employer; with no
    employer given the key is the owner's general answer, and with `NO_EMPLOYER` there is
    no key. A question matched by a rule that tolerates extra words ("...relocate to
    Austin?"), and any negated question, is remembered for its own wording: taking a
    negation out loses which negation it was.
    """
    if not question.answerable:
        return None
    scope = question.scope
    if scope == "employer":
        if employer == NO_EMPLOYER:
            return None
        scope = "employer:" + (employer or ANY_EMPLOYER)
    return hashlib.sha256(
        json.dumps([question.canonical_id, question.polarity, scope, question.variant]).encode()
    ).hexdigest()


TENANT_IN_PATH = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com")


def memory_keys(question: Question, employer: str = "") -> list[str]:
    """The keys to look an earlier answer up under: this employer's, then the general one."""
    keys = [memory_key(question, employer)]
    if question.scope == "employer" and employer:
        keys.append(memory_key(question))
    return [key for key in keys if key]


def employer_key(url) -> str:
    """Which employer an application belongs to, for answers that differ per employer.

    `NO_EMPLOYER` when the address does not say: a shared board without its tenant.
    """
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if not host:
        return NO_EMPLOYER
    for suffix in TENANT_IN_PATH:
        if host == suffix or host.endswith("." + suffix):
            tenant = next((p for p in parts.path.split("/") if p), "")
            if tenant in {"", "embed"}:
                tenant = parse_qs(parts.query).get("for", [""])[0]
            return f"{suffix}/{tenant.lower()}" if tenant else NO_EMPLOYER
    return re.sub(r"^(?:www|careers|jobs|apply)\.", "", host)


def ask_each_time(question: Question, profile: dict | None) -> bool:
    """A class the approved profile keeps with the owner on every form."""
    path = POLICY_KEYS.get(question.topic)
    if not path:
        return False
    fallback = ASK_EACH_TIME if question.topic == "export_control" else ""
    section = (profile or {}).get(path[0]) or {}
    return section.get(path[1], fallback) == ASK_EACH_TIME


def option_matches(option_label, value) -> bool:
    """Exact option text, the same words run together ("On-site", "Onsite"), or the same
    country written differently."""
    a, b = normalized(option_label), normalized(value)
    return (
        a == b
        or a.replace(" ", "") == b.replace(" ", "")
        or (a in COUNTRY_ALIASES and b in COUNTRY_ALIASES)
    )


def match_option(options, value, *, loose: bool = False) -> str | None:
    """The one offered option that is this value. `loose` also accepts the only option
    that starts with the same yes or no, when the opposite is offered too; a sensitive
    answer is never matched that way."""
    labels = [str(o) for o in options or []]
    exact = [o for o in labels if option_matches(o, value)]
    if exact:
        return exact[0] if len(exact) == 1 else None
    first = normalized(value).split()[:1]
    if loose and first in (["yes"], ["no"]):
        opposite = ["no"] if first == ["yes"] else ["yes"]
        same = [o for o in labels if normalized(o).split()[:1] == first]
        other = [o for o in labels if normalized(o).split()[:1] == opposite]
        if len(same) == 1 and other:
            return same[0]
    return None


def match_many(options, value) -> list[str] | None:
    """The boxes of a checkbox group an answer ticks, in the form's order.

    Every part of the answer must name an option. A lone box under a question is ticked
    by a plain yes.
    """
    labels = [str(o) for o in options or []]
    chosen = form_reading.match_options(value, labels)
    if chosen is None and len(labels) == 1 and normalized(value) in {"yes", "true"}:
        return labels
    return chosen


def option_labels(options) -> list[str]:
    return [str(o.get("label", "")) if isinstance(o, dict) else str(o) for o in options or []]


# --- a. the approved profile ---------------------------------------------------------
IDENTITY_KEYS = {
    "first_name": "legal_first_name",
    "middle_name": "legal_middle_name",
    "last_name": "legal_last_name",
    "preferred_name": "preferred_name",
    "email": "email",
    "phone": "phone",
    "location_city": "city",
    "state_region": "state_region",
    "country": "country",
    "postal_code": "postal_code",
    "linkedin_url": "linkedin",
    "github_url": "github",
    "portfolio_url": "portfolio",
}


# How forms spell the approved work styles; the first spelling is the one for a text box.
WORK_STYLE_WORDS = {
    "remote": ("Remote", "Fully remote", "Work from home"),
    "hybrid": ("Hybrid",),
    "onsite": ("On-site", "Onsite", "In office", "In-office", "In person", "In-person"),
}


def _yes_no(value) -> list[str] | None:
    return None if value is None else ["Yes" if value else "No"]


def _excluded_place(question: Question, profile: dict) -> bool:
    prefs = profile.get("preferences") or {}
    excluded = [normalized(x) for x in prefs.get("excluded_locations") or [] if x]
    return any(place and f" {place} " in f" {question.name} " for place in excluded)


def profile_fact(question: Question, profile: dict, has_options: bool = False):
    """(candidate values, source) from the approved profile, or None.

    A sensitive question is answered here only on an exact positive match of its
    canonical rule; a negated question is never answered from a profile fact.
    """
    if question.polarity != POSITIVE or not question.known:
        return None
    canonical = question.canonical_id
    identity = profile.get("identity") or {}
    if canonical in IDENTITY_KEYS:
        key = IDENTITY_KEYS[canonical]
        return ([str(identity[key])], "identity." + key) if identity.get(key) else None
    if canonical == "phone_type":
        return ["Mobile"], "default.phone_type"
    if canonical == "full_name":
        parts = [
            identity.get(k)
            for k in ("legal_first_name", "legal_middle_name", "legal_last_name")
            if identity.get(k)
        ]
        return ([" ".join(parts)], "identity.legal_name") if parts else None
    if canonical == "location":
        parts = [identity.get("city"), identity.get("state_region")]
        return ([", ".join(parts)], "identity.location") if all(parts) else None
    schools = (profile.get("education") or {}).get("schools") or []
    school = schools[0] if len(schools) == 1 else None
    if canonical in {"school", "major", "degree"}:
        if school and school.get(canonical):
            return [str(school[canonical])], "education.schools.0." + canonical
        return None
    if canonical == "gpa":
        if school and school.get("disclose_gpa") is True and school.get("gpa") is not None:
            return [str(school["gpa"])], "education.schools.0.gpa"
        return None
    if canonical == "graduation_date":
        month = school.get("graduation_month") if school else None
        if not month:
            return None
        when = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
        source = "education.schools.0.graduation_month"
        if has_options:
            return [when.strftime(f) for f in ("%B %Y", "%b %Y", "%Y")], source
        if question.name in GRADUATION_YEAR:
            return [when.strftime("%Y")], source
        return ([when.strftime("%B %Y")], source) if question.name in GRADUATION_DATE else None
    eligible = profile.get("eligibility") or {}
    if canonical == "work_authorization_us":
        values = _yes_no(eligible.get("us_work_authorized"))
        return (values, "eligibility.us_work_authorized") if values else None
    now, future = eligible.get("sponsorship_now"), eligible.get("sponsorship_future")
    if canonical == "work_authorization_us_without_sponsorship":
        authorized = eligible.get("us_work_authorized")
        if authorized is None or (authorized and (now is None or future is None)):
            return None
        free = bool(authorized) and now is False and future is False
        return _yes_no(free), "eligibility.us_work_authorized+sponsorship"
    if canonical.startswith("sponsorship") and question.scope in {"us", "unspecified"}:
        if question.detail == "now":
            values, source = _yes_no(now), "eligibility.sponsorship_now"
        elif question.detail == "future":
            values, source = _yes_no(future), "eligibility.sponsorship_future"
        else:
            source = "eligibility.sponsorship_now+future"
            if now is True or future is True:
                values = ["Yes"]
            else:
                values = ["No"] if now is False and future is False else None
        return (values, source) if values else None
    if canonical == "citizenship_us":
        values = _yes_no(eligible.get("us_citizen"))
        return (values, "eligibility.us_citizen") if values else None
    if canonical == "at_least_18":
        values = _yes_no(eligible.get("at_least_18"))
        return (values, "eligibility.at_least_18") if values else None
    prefs = profile.get("preferences") or {}
    if canonical == "relocate_willing":
        if question.detail == "risky" or _excluded_place(question, profile):
            # Another country, a cost condition, or a place the owner excluded.
            return None
        values = _yes_no(prefs.get("relocate"))
        return (values, "preferences.relocate") if values else None
    if canonical == "relocation_assistance_needed":
        values = _yes_no(prefs.get("relocation_support_required"))
        return (values, "preferences.relocation_support_required") if values else None
    if canonical == "onsite_willing":
        styles = prefs.get("work_styles") or []
        wanted = {"onsite", "hybrid"} if "hybrid" in question.name.split() else {"onsite"}
        if question.detail == "risky" or _excluded_place(question, profile):
            return None
        return (["Yes"], "preferences.work_styles") if wanted & set(styles) else None
    if canonical == "located_in_us":
        country = normalized(identity.get("country"))
        if not country:
            return None
        return _yes_no(country in COUNTRY_ALIASES), "identity.country"
    availability = profile.get("availability") or {}
    if canonical in {"start_availability", "availability_end"}:
        key = "earliest_start" if canonical == "start_availability" else "latest_end"
        day = availability.get(key)
        if not day:
            return None
        when = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
        return [f"{when.strftime('%B')} {when.day}, {when.year}", day], (
            "profile.availability." + key
        )
    if canonical == "hours_per_week":
        hours = availability.get("hours_per_week")
        return ([str(hours)], "profile.availability.hours_per_week") if hours else None
    if canonical == "preferred_locations":
        places = [str(p) for p in prefs.get("preferred_locations") or [] if p]
        # The joined list for a text box first; one place alone fits an office option.
        return ([", ".join(places), *places], "preferences.preferred_locations") if places else None
    if canonical == "work_style_preference":
        styles = [s for s in prefs.get("work_styles") or [] if s in WORK_STYLE_WORDS]
        if not styles:
            return None
        words = [WORK_STYLE_WORDS[s][0] for s in styles]
        spellings = [spelling for s in styles for spelling in WORK_STYLE_WORDS[s]]
        return [", ".join(words), *spellings], "preferences.work_styles"
    if canonical in {"talent_network_opt_in", "marketing_opt_in"}:
        values = _yes_no((profile.get("application_policy") or {}).get(canonical))
        return (values, "profile.application_policy." + canonical) if values else None
    return None


def known_fact(label, profile: dict) -> tuple[str | None, str | None]:
    """A free-text value for this label from the approved profile, with its source."""
    question = classify(label)
    if not question.answerable:
        return None, None
    fact = profile_fact(question, profile)
    return (fact[0][0], fact[1]) if fact else (None, None)


# --- decline, defaults ---------------------------------------------------------------
def decline_self_identification(label, options) -> str | None:
    """Voluntary self-identification is answered with the form's own decline option.

    Declining cannot affect candidacy; it is the standard practice, never an inference
    about the applicant. A form without a decline option stays with the owner.
    """
    if not SELF_ID.search(plain_name(label)):
        return None
    matches = [str(o) for o in options or [] if DECLINE_OPTION.search(str(o))]
    return matches[0] if len(matches) == 1 else None


def policy_default(question: Question, options, profile: dict, kind: str = ""):
    """(value, source) from the owner's standing rules for plain questions, or None."""
    if (
        question.sensitivity != PLAIN
        or question.polarity != POSITIVE
        or not question.answerable
        or not question.default
        or _excluded_place(question, profile)
    ):
        return None
    labels = option_labels(options)
    source = "policy.default." + question.default
    if question.default == "how_did_you_hear":
        if not labels:
            return (HEARD_TEXT, source) if kind in {"", "text", "textarea"} else None
        for tier in HEARD_TIERS:
            matches = [o for o in labels if tier.search(o)]
            if len(matches) == 1:
                return matches[0], source
        return None
    answer = DEFAULTS.get(question.default)
    if answer is None:
        return None
    if question.default == "onsite_willing":
        styles = (profile.get("preferences") or {}).get("work_styles") or []
        if styles and not {"onsite", "hybrid"} & set(styles):
            return None  # the approved profile says remote only: not a yes by default
    if kind == "checkbox_group" and len(labels) == 1:
        return answer, source  # a lone box under the question: yes ticks it
    if labels:
        chosen = match_option(labels, answer, loose=True)
        return (chosen, source) if chosen else None
    # Without options only a checkbox takes a bare yes; a text box wants a sentence.
    return (answer, source) if kind == "checkbox" else None


# --- d. the model, and the gate in front of it ---------------------------------------
def unlabeled(field: dict) -> dict:
    """The marker a pending question carries when the form gave its field no label."""
    return {"label_missing": True} if field.get("label_missing") else {}


def draft_gate(question: dict | None) -> dict | None:
    """Why a model draft may not answer this question, or None when it may.

    `question` is a pending-question or observed-field dict. `words` is for the owner;
    `code` is for the system log.
    """
    if not question:
        return None
    if form_reading.unreadable(question) or not plain_name(question.get("label")):
        return {
            "code": "label_missing",
            "words": "The form's text for this question could not be read, so only you can "
            "say what belongs in it.",
        }
    classified = classify(
        question.get("label"), question.get("kind") or "", option_labels(question.get("options"))
    )
    if classified.sensitivity == SENSITIVE:
        return {
            "code": f"sensitive:{classified.topic or classified.canonical_id}",
            "words": "This is a legal or personal question, so only your own answer is used.",
        }
    return None


def application_answer(field: dict, answers: dict, automatic: dict) -> dict | None:
    """This application's own answer for a field: the owner's, else what Rove recorded.

    A draft Rove used on its own never stands in for the owner on a sensitive or
    unlabeled question, even if one was stored before the gate existed.
    """
    given = answers.get(field["key"])
    if given:
        return given
    recorded = automatic.get(field["key"])
    if recorded and recorded.get("kind") == "draft" and draft_gate(field):
        return None
    return recorded


def fills_long_text(source) -> bool:
    """A remembered answer or a default may fill a long-text field; a profile fact may not."""
    return source == REMEMBERED or str(source or "").startswith("policy.default.")


# --- the resolver --------------------------------------------------------------------
def resolve(
    field: dict, profile: dict, *, recall=None, employer: str = "", picker: bool = False
) -> tuple[str | None, str | None]:
    """(value, source) for one observed field from the first source allowed to answer.

    `recall(label, options, kind=, employer=, profile=)` returns the owner's earlier answer.
    `picker` marks a control whose options are not known yet: only values that do not
    depend on the option list are returned, and the caller resolves again once it has
    read the options. Qwen and the owner come after this function, in the worker.
    """
    label, kind = field.get("label") or "", field.get("kind") or ""
    if field.get("label_missing"):
        return None, None
    labels = option_labels(field.get("options"))
    question = classify(label, kind, labels)
    if not question.answerable:
        return None, None
    loose = question.sensitivity == PLAIN
    fact = profile_fact(question, profile, bool(labels))
    if fact:
        values, source = fact
        if not labels:
            return values[0], source
        chosen = {match_option(labels, v, loose=loose) for v in values} - {None}
        if len(chosen) == 1:
            return chosen.pop(), source
    policy = POLICY_KEYS.get(question.topic)
    section = (profile.get(policy[0]) or {}) if policy else {}
    decline_first = bool(policy) and section.get(policy[1]) == "decline_when_optional"
    declined = None if picker else decline_self_identification(label, labels)
    if declined and decline_first:
        return declined, DECLINED
    if recall is not None and not ask_each_time(question, profile):
        remembered = recall(label, labels, kind=kind, employer=employer, profile=profile)
        if remembered is not None:
            return remembered, REMEMBERED
    if declined:
        return declined, DECLINED
    if picker:
        return None, None
    default = policy_default(question, labels, profile, kind)
    return default if default else (None, None)
