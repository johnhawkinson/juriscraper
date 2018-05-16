import pprint
import re
import sys
from datetime import date

import feedparser
from requests import Session

from .docket_report import DocketReport
from .utils import clean_pacer_object, get_pacer_case_id_from_docket_url, \
    get_pacer_doc_id_from_doc1_url
from ..lib.html_utils import html_unescape
from ..lib.log_tools import make_default_logger
from ..lib.string_utils import harmonize, clean_string

logger = make_default_logger()

"""
As of 2018-04-25, these jurisdictions do not have functional RSS feeds:

'miwb', 'nceb', 'nmib', 'alnd', 'caed', 'flnd', 'gand', 'gasd', 'hid', 'ilsd',
'kyed', 'mdd', 'msnd', 'mtd', 'nvd', 'nywd', 'ndd', 'oked', 'oknd', 'pamd',
'scd', 'tnwd', 'txnd', 'vaed'

I reached out to all of these jurisdictions and heard back the following:

 - vaed: "We are currently looking into this and will possibly have this
   feature in the near future."
 - txnd: Left me a long voicemail. They're working on it and have it in
   committee. Expectation is that they may require an en banc meeting of the
   judges to make a decision, but that the decision should come soon and be
   positive.

"""


class PacerRssFeed(DocketReport):
    document_number_regex = re.compile(r'">(\d+)</a>')
    doc1_url_regex = re.compile(r'href="(.*)"')
    short_desc_regex = re.compile(r'\[(.*?)\] \(')  # Matches 'foo': [ foo ] (

    PATH = 'cgi-bin/rss_outside.pl'

    CACHE_ATTRS = ['data']

    def __init__(self, court_id):
        super(PacerRssFeed, self).__init__(court_id)
        self._clear_caches()
        self._data = None
        self.session = Session()
        self.is_valid = True

        if self.court_id.endswith('b'):
            self.is_bankruptcy = True
        else:
            self.is_bankruptcy = False

    @property
    def url(self):
        if self.court_id == 'ilnb':
            return "https://tdi.ilnb.uscourts.gov/wwwroot/RSS/rss_outside.xml"
        else:
            return "https://ecf.%s.uscourts.gov/%s" % \
                (self.court_id, self.PATH)

    def query(self):
        """Query the RSS feed for a given court ID

        Note that we use requests here, and so we forgo some of the
        useful features that feedparser offers around the Etags and
        Last-Modified headers. This is fine for now because no PACER
        site seems to support these headers, but eventually we'll
        probably want to do better here. The reason we *don't* use
        feedparser already is because it presents a different set of
        APIs and Exceptions that we don't want to monkey with, at
        least, not yet, and especially not yet if PACER itself doesn't
        seem to care.

        For a good summary of this issue, see:
        https://github.com/freelawproject/juriscraper/issues/195#issuecomment-385848344
        """
        logger.info(u"Querying the RSS feed for %s" % self.court_id)
        timeout = (60, 300)
        self.response = self.session.get(self.url, timeout=timeout)

    def parse(self):
        self._clear_caches()
        self.response.raise_for_status()
        self._parse_text(self.response.text)

    def _parse_text(self, text):
        """Parse the feed and set self.feed

        :param text: The text of the RSS feed.
        :return None
        """
        self.feed = feedparser.parse(text)

    @property
    def data(self):
        """Return a list of docket-like objects instead of the usual dict that
         is usually provided by the BaseDocketReport superclass.

        When CMECF generates the RSS feed, it breaks up items with
        multiple consecutive entries into multiple RSS items with
        identical timestamp/id/title.  We reverse that and recombine
        those items.
        """
        if self._data is not None:
            return self._data

        def twin_entries(a, b):
            fields = ['title', 'link', 'id', 'published']
            matching_fields = (a[f] == b[f] for f in fields)
            return all(matching_fields)

        data_list = []
        prevdata = None
        preventry = None
        for entry in self.feed.entries:
            data = self.metadata(entry)

            de = self.docket_entries(entry)
            # If this entry and the immediately prior entry match
            # in metadata, then add the current description to
            # the previous entry's and continue the loop.
            if (
                    preventry and
                    prevdata[u'docket_entries'] and
                    twin_entries(entry, preventry) and
                    len(de) > 0  # xxx
            ):
                # xxx we rely on the fact that there's only ever one
                # item in this array, which is true but flawed
                prevdata['docket_entries'][0][u'short_description'] += (
                    ' AND ' + de[0][u'short_description'])
                continue

            data[u'parties'] = None
            data[u'docket_entries'] = self.docket_entries(entry)
            if data[u'docket_entries'] and data[u'docket_number']:
                data_list.append(data)

            preventry = entry
            prevdata = data

        self._data = data_list
        return data_list

    def metadata(self, entry):
        data = {
            u'court_id': self.court_id,
            u'pacer_case_id': get_pacer_case_id_from_docket_url(entry.link),
            u'docket_number': self._get_docket_number(entry.title),
            u'case_name': self._get_case_name(entry.title),
            # Filing date is not available. Also the case for free opinions.
            u'date_filed': None,
            u'date_terminated': None,
            u'date_converted': None,
            u'date_discharged': None,
            u'assigned_to_str': '',
            u'referred_to_str': '',
            u'cause': '',
            u'nature_of_suit': '',
            u'jury_demand': '',
            u'demand': '',
            u'jurisdiction': '',
        }
        data = clean_pacer_object(data)
        return data

    @property
    def parties(self):
        raise NotImplementedError("No parties for RSS feeds.")

    def docket_entries(self, entry):
        """Parse the RSS item to get back a docket entry-like object"""
        de = {
            u'date_filed': date(*entry.published_parsed[:3]),
            u'document_number': self._get_value(self.document_number_regex,
                                                entry.summary),
            u'description': '',
            u'short_description': html_unescape(
                self._get_value(self.short_desc_regex, entry.summary)),
        }
        doc1_url = self._get_value(self.doc1_url_regex, entry.summary)
        if not all([doc1_url.strip(), de['document_number']]):
            return []

        de[u'pacer_doc_id'] = get_pacer_doc_id_from_doc1_url(doc1_url)

        return [de]

    def _get_docket_number(self, title_text):
        if self.is_bankruptcy:
            # Uses both b/c sometimes the bankr. cases have a dist-style docket
            # number.
            regexes = [self.docket_number_dist_regex,
                       self.docket_number_bankr_regex]
        else:
            regexes = [self.docket_number_dist_regex]
        for regex in regexes:
            match = regex.search(title_text)
            if match:
                return match.group(1)

    def _get_case_name(self, title_text):
        # 1:18-cv-04423 Chau v. Gorg &amp; Smith et al --> Chau v. Gorg & Smith
        try:
            case_name = title_text.split(' ', 1)[1]
        except IndexError:
            return u"Unknown Case Title"
        case_name = html_unescape(case_name)
        case_name = clean_string(harmonize(case_name))
        return case_name


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m juriscraper.pacer.rss_feeds [pacer_court_id] "
              "[verbose]")
        print("Please provide a valid PACER court id as your only argument")
        sys.exit(1)
    feed = PacerRssFeed(sys.argv[1])
    print("Querying RSS feed at: %s" % feed.url)
    feed.query()
    print("Parsing RSS feed for %s" % feed.court_id)
    feed.parse()
    print("Got %s items" % len(feed.data))
    if len(sys.argv) == 3 and sys.argv[2] == 'verbose':
        print("Here they are:\n")
        pprint.pprint(feed.data, indent=2)
