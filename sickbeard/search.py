# Author: Nic Wolfe <nic@wolfeden.ca>
# URL: http://code.google.com/p/sickbeard/
#
# This file is part of Sick Beard.
#
# Sick Beard is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Sick Beard is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Sick Beard.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import with_statement

import os
import re
import threading
import traceback
import datetime

import sickbeard

from common import SNATCHED, SNATCHED_PROPER, SNATCHED_BEST, Quality, SEASON_RESULT, MULTI_EP_RESULT, Overview

from sickbeard import logger, db, show_name_helpers, exceptions, helpers
from sickbeard import sab
from sickbeard import nzbget
from sickbeard import clients
from sickbeard import history
from sickbeard import notifiers
from sickbeard import nzbSplitter
from sickbeard import ui
from sickbeard import encodingKludge as ek
from sickbeard import providers
from sickbeard import failed_history
from sickbeard.exceptions import ex
from sickbeard.providers.generic import GenericProvider, tvcache

def _downloadResult(result):
    """
    Downloads a result to the appropriate black hole folder.

    Returns a bool representing success.

    result: SearchResult instance to download.
    """

    resProvider = result.provider

    newResult = False

    if resProvider == None:
        logger.log(u"Invalid provider name - this is a coding error, report it please", logger.ERROR)
        return False

    # nzbs with an URL can just be downloaded from the provider
    if result.resultType == "nzb":
        newResult = resProvider.downloadResult(result)

    # if it's an nzb data result
    elif result.resultType == "nzbdata":

        # get the final file path to the nzb
        fileName = ek.ek(os.path.join, sickbeard.NZB_DIR, result.name + ".nzb")

        logger.log(u"Saving NZB to " + fileName)

        newResult = True

        # save the data to disk
        try:
            with ek.ek(open, fileName, 'w') as fileOut:
                fileOut.write(result.extraInfo[0])

            helpers.chmodAsParent(fileName)

        except EnvironmentError, e:
            logger.log(u"Error trying to save NZB to black hole: " + ex(e), logger.ERROR)
            newResult = False

    elif resProvider.providerType == "torrent":
        newResult = resProvider.downloadResult(result)

    else:
        logger.log(u"Invalid provider type - this is a coding error, report it please", logger.ERROR)
        return False

    if newResult and sickbeard.USE_FAILED_DOWNLOADS:
        ui.notifications.message('Episode snatched',
                                 '<b>%s</b> snatched from <b>%s</b>' % (result.name, resProvider.name))

    return newResult


def snatchEpisode(result, endStatus=SNATCHED):
    """
    Contains the internal logic necessary to actually "snatch" a result that
    has been found.

    Returns a bool representing success.

    result: SearchResult instance to be snatched.
    endStatus: the episode status that should be used for the episode object once it's snatched.
    """

    if result is None: return False

    # don't notify when we re-download an episode
    for curEpObj in result.episodes:
        if curEpObj.status in Quality.SNATCHED + Quality.SNATCHED_PROPER + Quality.SNATCHED_BEST:
            return 2

    result.priority = 0  # -1 = low, 0 = normal, 1 = high
    if sickbeard.ALLOW_HIGH_PRIORITY:
        # if it aired recently make it high priority
        for curEp in result.episodes:
            if datetime.date.today() - curEp.airdate <= datetime.timedelta(days=7):
                result.priority = 1
    if re.search('(^|[\. _-])(proper|repack)([\. _-]|$)', result.name, re.I) != None:
        endStatus = SNATCHED_PROPER

    # NZBs can be sent straight to SAB or saved to disk
    if result.resultType in ("nzb", "nzbdata"):
        if sickbeard.NZB_METHOD == "blackhole":
            dlResult = _downloadResult(result)
        elif sickbeard.NZB_METHOD == "sabnzbd":
            dlResult = sab.sendNZB(result)
        elif sickbeard.NZB_METHOD == "nzbget":
            is_proper = True if endStatus == SNATCHED_PROPER else False
            dlResult = nzbget.sendNZB(result, is_proper)
        else:
            logger.log(u"Unknown NZB action specified in config: " + sickbeard.NZB_METHOD, logger.ERROR)
            dlResult = False

    # TORRENTs can be sent to clients or saved to disk
    elif result.resultType == "torrent":
        # torrents are saved to disk when blackhole mode
        if sickbeard.TORRENT_METHOD == "blackhole":
            dlResult = _downloadResult(result)
        else:
            result.content = result.provider.getURL(result.url) if not result.url.startswith('magnet') else None
            client = clients.getClientIstance(sickbeard.TORRENT_METHOD)()
            dlResult = client.sendTORRENT(result)
    else:
        logger.log(u"Unknown result type, unable to download it", logger.ERROR)
        dlResult = False

    if not dlResult:
        return False

    if sickbeard.USE_FAILED_DOWNLOADS:
        failed_history.logSnatch(result)
    else:
        ui.notifications.message('Episode snatched', result.name)

    history.logSnatch(result)

    # don't notify when we re-download an episode
    for curEpObj in result.episodes:
        with curEpObj.lock:
            if isFirstBestMatch(result):
                curEpObj.status = Quality.compositeStatus(SNATCHED_BEST, result.quality)
            else:
                curEpObj.status = Quality.compositeStatus(endStatus, result.quality)
            curEpObj.saveToDB()

        if curEpObj.status not in Quality.DOWNLOADED:
            notifiers.notify_snatch(curEpObj._format_pattern('%SN - %Sx%0E - %EN - %QN'))

    return True


def searchForNeededEpisodes(curProvider):
    threading.currentThread().name = curProvider.name

    logger.log(u"Searching all providers for any needed episodes")
    foundResults = {}

    # ask all providers for any episodes it finds
    try:
        curFoundResults = curProvider.searchRSS()
    except exceptions.AuthException, e:
        logger.log(u"Authentication error: " + ex(e), logger.ERROR)
        return
    except Exception, e:
        logger.log(u"Error while searching " + curProvider.name + ", skipping: " + ex(e), logger.ERROR)
        logger.log(traceback.format_exc(), logger.DEBUG)
        return

    didSearch = True

    # pick a single result for each episode, respecting existing results
    for curEp in curFoundResults:

        if curEp.show.paused:
            logger.log(u"Show " + curEp.show.name + " is paused, ignoring all RSS items for " + curEp.prettyName(),
                       logger.DEBUG)
            continue

        # find the best result for the current episode
        bestResult = None
        for curResult in curFoundResults[curEp]:
            if not bestResult or bestResult.quality < curResult.quality:
                bestResult = curResult

        bestResult = pickBestResult(curFoundResults[curEp], curEp.show)

        # if all results were rejected move on to the next episode
        if not bestResult:
            logger.log(u"All found results for " + curEp.prettyName() + " were rejected.", logger.DEBUG)
            continue

        # if it's already in the list (from another provider) and the newly found quality is no better then skip it
        if curEp in foundResults and bestResult.quality <= foundResults[curEp].quality:
            continue

        foundResults[curEp] = bestResult

    if not didSearch:
        logger.log(u"No NZB/Torrent providers found or enabled in the sickbeard config. Please check your settings.",
                   logger.ERROR)

    return foundResults.values()

def filter_release_name(name, filter_words):
    """
    Filters out results based on filter_words

    name: name to check
    filter_words : Words to filter on, separated by comma

    Returns: False if the release name is OK, True if it contains one of the filter_words
    """
    if filter_words:
        filters = [re.compile('(^|[\W_])%s($|[\W_])' % filter.strip(), re.I) for filter in filter_words.split(',')]
        for regfilter in filters:
            if regfilter.search(name):
                logger.log(u"" + name + " contains pattern: " + regfilter.pattern, logger.DEBUG)
                return True

    return False

def pickBestResult(results, show, quality_list=None):
    logger.log(u"Picking the best result out of " + str([x.name for x in results]), logger.DEBUG)

    # find the best result for the current episode
    bestResult = None
    for cur_result in results:
        logger.log("Quality of " + cur_result.name + " is " + Quality.qualityStrings[cur_result.quality])

        if quality_list and cur_result.quality not in quality_list:
            logger.log(cur_result.name + " is a quality we know we don't want, rejecting it", logger.DEBUG)
            continue

        if show.rls_ignore_words and filter_release_name(cur_result.name, show.rls_ignore_words):
            logger.log(u"Ignoring " + cur_result.name + " based on ignored words filter: " + show.rls_ignore_words,
                       logger.MESSAGE)
            continue

        if show.rls_require_words and not filter_release_name(cur_result.name, show.rls_require_words):
            logger.log(u"Ignoring " + cur_result.name + " based on required words filter: " + show.rls_require_words,
                       logger.MESSAGE)
            continue

        if sickbeard.USE_FAILED_DOWNLOADS and failed_history.hasFailed(cur_result.name, cur_result.size,
                                                                       cur_result.provider.name):
            logger.log(cur_result.name + u" has previously failed, rejecting it")
            continue

        if not bestResult or bestResult.quality < cur_result.quality and cur_result.quality != Quality.UNKNOWN:
            bestResult = cur_result
        elif bestResult.quality == cur_result.quality:
            if "proper" in cur_result.name.lower() or "repack" in cur_result.name.lower():
                bestResult = cur_result
            elif "internal" in bestResult.name.lower() and "internal" not in cur_result.name.lower():
                bestResult = cur_result

    if bestResult:
        logger.log(u"Picked " + bestResult.name + " as the best", logger.DEBUG)
    else:
        logger.log(u"No result picked.", logger.DEBUG)

    return bestResult


def isFinalResult(result):
    """
    Checks if the given result is good enough quality that we can stop searching for other ones.

    If the result is the highest quality in both the any/best quality lists then this function
    returns True, if not then it's False

    """

    logger.log(u"Checking if we should keep searching after we've found " + result.name, logger.DEBUG)

    show_obj = result.episodes[0].show

    any_qualities, best_qualities = Quality.splitQuality(show_obj.quality)

    # if there is a redownload that's higher than this then we definitely need to keep looking
    if best_qualities and result.quality < max(best_qualities):
        return False

    # if there's no redownload that's higher (above) and this is the highest initial download then we're good
    elif any_qualities and result.quality == max(any_qualities):
        return True

    elif best_qualities and result.quality == max(best_qualities):

        # if this is the best redownload but we have a higher initial download then keep looking
        if any_qualities and result.quality < max(any_qualities):
            return False

        # if this is the best redownload and we don't have a higher initial download then we're done
        else:
            return True

    # if we got here than it's either not on the lists, they're empty, or it's lower than the highest required
    else:
        return False


def isFirstBestMatch(result):
    """
    Checks if the given result is a best quality match and if we want to archive the episode on first match.
    """

    logger.log(u"Checking if we should archive our first best quality match for for episode " + result.name,
               logger.DEBUG)

    show_obj = result.episodes[0].show

    any_qualities, best_qualities = Quality.splitQuality(show_obj.quality)

    # if there is a redownload that's a match to one of our best qualities and we want to archive the episode then we are done
    if best_qualities and show_obj.archive_firstmatch and result.quality in best_qualities:
        return True

    return False

def filterSearchResults(show, results):
    foundResults = {}

    # make a list of all the results for this provider
    for curEp in results.keys():
        # skip non-tv crap
        results[curEp] = filter(
            lambda x: show_name_helpers.filterBadReleases(x.name) and show_name_helpers.isGoodResult(x.name, show),
            results[curEp])

        if len(results[curEp]):
            if curEp in foundResults:
                foundResults[curEp] += results[curEp]
            else:
                foundResults[curEp] = results[curEp]

    return foundResults

def searchProviders(show, season, episodes, curProvider, seasonSearch=False, manualSearch=False):
    threading.currentThread().name = curProvider.name

    logger.log(u"Searching for stuff we need from " + show.name + " season " + str(season))
    foundResults = {}
    finalResults = []

    if manualSearch:
        curProvider.cache.updateCache()

    # convert indexer numbering to scene numbering for searches
    map(lambda x: x.convertToSceneNumbering, episodes)

    try:
        curResults = curProvider.findSearchResults(show, season, episodes, seasonSearch, manualSearch)
    except exceptions.AuthException, e:
        logger.log(u"Authentication error: " + ex(e), logger.ERROR)
        return []
    except Exception, e:
        logger.log(u"Error while searching " + curProvider.name + ", skipping: " + ex(e), logger.ERROR)
        logger.log(traceback.format_exc(), logger.DEBUG)
        return []

    if not len(curResults):
        return []

    curResults = filterSearchResults(show, curResults)
    if len(curResults):
        foundResults.update(curResults)
        logger.log(u"Provider search results: " + str(foundResults), logger.DEBUG)

    if not len(foundResults):
        return []

    anyQualities, bestQualities = Quality.splitQuality(show.quality)

    # pick the best season NZB
    bestSeasonNZB = None
    if SEASON_RESULT in foundResults:
        bestSeasonNZB = pickBestResult(foundResults[SEASON_RESULT], show, anyQualities + bestQualities)

    highest_quality_overall = 0
    for cur_episode in foundResults:
        for cur_result in foundResults[cur_episode]:
            if cur_result.quality != Quality.UNKNOWN and cur_result.quality > highest_quality_overall:
                highest_quality_overall = cur_result.quality
    logger.log(u"The highest quality of any match is " + Quality.qualityStrings[highest_quality_overall], logger.DEBUG)

    # see if every episode is wanted
    if bestSeasonNZB:

        # get the quality of the season nzb
        seasonQual = Quality.sceneQuality(bestSeasonNZB.name)
        seasonQual = bestSeasonNZB.quality
        logger.log(
            u"The quality of the season " + bestSeasonNZB.provider.providerType + " is " + Quality.qualityStrings[
                seasonQual], logger.DEBUG)

        myDB = db.DBConnection()
        allEps = [int(x["episode"]) for x in
                  myDB.select("SELECT episode FROM tv_episodes WHERE showid = ? AND season = ?",
                              [show.indexerid, season])]
        logger.log(u"Episode list: " + str(allEps), logger.DEBUG)

        allWanted = True
        anyWanted = False
        for curEpNum in allEps:
            if not show.wantEpisode(season, curEpNum, seasonQual):
                allWanted = False
            else:
                anyWanted = True

        # if we need every ep in the season check if single episode releases should be preferred over season releases (missing single episode releases will be picked individually from season release)
        preferSingleEpisodesOverSeasonReleases = sickbeard.PREFER_EPISODE_RELEASES
        logger.log(u"Prefer single episodes over season releases: "+str(preferSingleEpisodesOverSeasonReleases), logger.DEBUG)
        # if we need every ep in the season and there's nothing better then just download this and be done with it (unless single episodes are preferred)
        if allWanted and bestSeasonNZB.quality == highest_quality_overall and not preferSingleEpisodesOverSeasonReleases:
            logger.log(u"Every ep in this season is needed, downloading the whole " + bestSeasonNZB.provider.providerType + " " + bestSeasonNZB.name)
            epObjs = []
            for curEpNum in allEps:
                epObjs.append(show.getEpisode(season, curEpNum))
            bestSeasonNZB.episodes = epObjs
            return [bestSeasonNZB]

        elif not anyWanted:
            logger.log(
                u"No eps from this season are wanted at this quality, ignoring the result of " + bestSeasonNZB.name,
                logger.DEBUG)

        else:

            if bestSeasonNZB.provider.providerType == GenericProvider.NZB:
                logger.log(u"Breaking apart the NZB and adding the individual ones to our results", logger.DEBUG)

                # if not, break it apart and add them as the lowest priority results
                individualResults = nzbSplitter.splitResult(bestSeasonNZB)

                individualResults = filter(
                    lambda x: show_name_helpers.filterBadReleases(x.name) and show_name_helpers.isGoodResult(x.name,
                                                                                                             show),
                    individualResults)

                for curResult in individualResults:
                    if len(curResult.episodes) == 1:
                        epNum = curResult.episodes[0].episode
                    elif len(curResult.episodes) > 1:
                        epNum = MULTI_EP_RESULT

                    if epNum in foundResults:
                        foundResults[epNum].append(curResult)
                    else:
                        foundResults[epNum] = [curResult]

            # If this is a torrent all we can do is leech the entire torrent, user will have to select which eps not do download in his torrent client
            else:

                # Season result from Torrent Provider must be a full-season torrent, creating multi-ep result for it.
                logger.log(
                    u"Adding multi-ep result for full-season torrent. Set the episodes you don't want to 'don't download' in your torrent client if desired!")
                epObjs = []
                for curEpNum in allEps:
                    epObjs.append(show.getEpisode(season, curEpNum))
                bestSeasonNZB.episodes = epObjs

                epNum = MULTI_EP_RESULT
                if epNum in foundResults:
                    foundResults[epNum].append(bestSeasonNZB)
                else:
                    foundResults[epNum] = [bestSeasonNZB]

    # go through multi-ep results and see if we really want them or not, get rid of the rest
    multiResults = {}
    if MULTI_EP_RESULT in foundResults:
        for multiResult in foundResults[MULTI_EP_RESULT]:

            logger.log(u"Seeing if we want to bother with multi-episode result " + multiResult.name, logger.DEBUG)

            if sickbeard.USE_FAILED_DOWNLOADS and failed_history.hasFailed(multiResult.name, multiResult.size,
                                                                           multiResult.provider.name):
                logger.log(multiResult.name + u" has previously failed, rejecting this multi-ep result")
                continue

            # see how many of the eps that this result covers aren't covered by single results
            neededEps = []
            notNeededEps = []
            for epObj in multiResult.episodes:
                epNum = epObj.episode
                # if we have results for the episode
                if epNum in foundResults and len(foundResults[epNum]) > 0:
                    # but the multi-ep is worse quality, we don't want it
                    # TODO: wtf is this False for
                    #if False and multiResult.quality <= pickBestResult(foundResults[epNum]):
                    #    notNeededEps.append(epNum)
                    #else:
                    neededEps.append(epNum)
                else:
                    neededEps.append(epNum)

            logger.log(
                u"Single-ep check result is neededEps: " + str(neededEps) + ", notNeededEps: " + str(notNeededEps),
                logger.DEBUG)

            if not neededEps:
                logger.log(u"All of these episodes were covered by single nzbs, ignoring this multi-ep result",
                           logger.DEBUG)
                continue

            # check if these eps are already covered by another multi-result
            multiNeededEps = []
            multiNotNeededEps = []
            for epObj in multiResult.episodes:
                epNum = epObj.episode
                if epNum in multiResults:
                    multiNotNeededEps.append(epNum)
                else:
                    multiNeededEps.append(epNum)

            logger.log(
                u"Multi-ep check result is multiNeededEps: " + str(multiNeededEps) + ", multiNotNeededEps: " + str(
                    multiNotNeededEps), logger.DEBUG)

            if not multiNeededEps:
                logger.log(
                    u"All of these episodes were covered by another multi-episode nzbs, ignoring this multi-ep result",
                    logger.DEBUG)
                continue

            # if we're keeping this multi-result then remember it
            for epObj in multiResult.episodes:
                multiResults[epObj.episode] = multiResult

            # don't bother with the single result if we're going to get it with a multi result
            for epObj in multiResult.episodes:
                epNum = epObj.episode
                if epNum in foundResults:
                    logger.log(u"A needed multi-episode result overlaps with a single-episode result for ep #" + str(
                        epNum) + ", removing the single-episode results from the list", logger.DEBUG)
                    del foundResults[epNum]

    finalResults += set(multiResults.values())

    # of all the single ep results narrow it down to the best one for each episode
    for curEp in foundResults:
        if curEp in (MULTI_EP_RESULT, SEASON_RESULT):
            continue

        if len(foundResults[curEp]) == 0:
            continue

        finalResults.append(pickBestResult(foundResults[curEp], show))

    return finalResults
