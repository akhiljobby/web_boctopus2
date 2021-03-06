#!/usr/bin/env python
# Description: daemon to submit jobs and retrieve results to/from remote
#              servers
# 
# submit job, 
# get finished jobids 
# try to retrieve jobs with in finished jobids
import os
import sys
import site

rundir = os.path.dirname(os.path.realpath(__file__))
webserver_root = os.path.realpath("%s/../../../"%(rundir))

activate_env="%s/env/bin/activate_this.py"%(webserver_root)
execfile(activate_env, dict(__file__=activate_env))
#Add the site-packages of the virtualenv
site.addsitedir("%s/env/lib/python2.7/site-packages/"%(webserver_root))
sys.path.append("%s/env/lib/python2.7/site-packages/"%(webserver_root))

import myfunc
import time
from datetime import datetime
import requests
import json
import urllib
import shutil
import hashlib
import subprocess
from suds.client import Client

os.environ['TZ'] = 'Europe/Stockholm'
time.tzset()

vip_user_list = [
        "nanjiang.shu@scilifelab.se"
        ]
DEBUG = False

# make sure that only one instance of the script is running
# this code is working 
progname = os.path.basename(__file__)
lockname = progname.replace(" ", "").replace("/", "-")
import fcntl
lock_file = "/tmp/%s.lock"%(lockname)
fp = open(lock_file, 'w')
try:
    fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
except IOError:
    print >> sys.stderr, "Another instance of %s is running"%(progname)
    sys.exit(1)

contact_email = "nanjiang.shu@scilifelab.se"

threshold_logfilesize = 20*1024*1024

usage_short="""
Usage: %s
"""%(sys.argv[0])

usage_ext="""
Description:
    Daemon to submit jobs and retrieve results to/from remote servers
    run periodically
    At the end of each run generate a runlog file with the status of all jobs

OPTIONS:
  -h, --help    Print this help message and exit

Created 2015-03-25, updated 2015-03-25, Nanjiang Shu
"""
usage_exp="""
"""

basedir = os.path.realpath("%s/.."%(rundir)) # path of the application, i.e. pred/
path_log = "%s/static/log"%(basedir)
path_result = "%s/static/result"%(basedir)
path_md5cache = "%s/static/md5"%(basedir)
computenodefile = "%s/static/computenode.txt"%(basedir)
MAX_SUBMIT_JOB_PER_NODE = 200
blastdir = "%s/%s"%(rundir, "soft/topcons2_webserver/tools/blast-2.2.26")
os.environ['SCAMPI_DIR'] = "/server/scampi"
os.environ['MODHMM_BIN'] = "/server/modhmm/bin"
os.environ['BLASTMAT'] = "%s/data"%(blastdir)
os.environ['BLASTBIN'] = "%s/bin"%(blastdir)
os.environ['BLASTDB'] = "%s/%s"%(rundir, "soft/topcons2_webserver/database/blast/")
script_scampi = "%s/%s"%(rundir, "mySCAMPI_run.pl")
gen_errfile = "%s/static/log/%s.err"%(basedir, progname)
gen_logfile = "%s/static/log/%s.log"%(basedir, progname)
SLEEP_INTERVAL = 20 # sleep interval in seconds

def PrintHelp(fpout=sys.stdout):#{{{
    print >> fpout, usage_short
    print >> fpout, usage_ext
    print >> fpout, usage_exp#}}}

def get_job_status(jobid):#{{{
    status = "";
    rstdir = "%s/%s"%(path_result, jobid)
    starttagfile = "%s/%s"%(rstdir, "runjob.start")
    finishtagfile = "%s/%s"%(rstdir, "runjob.finish")
    failedtagfile = "%s/%s"%(rstdir, "runjob.failed")
    if os.path.exists(failedtagfile):
        status = "Failed"
    elif os.path.exists(finishtagfile):
        status = "Finished"
    elif os.path.exists(starttagfile):
        status = "Running"
    elif os.path.exists(rstdir):
        status = "Wait"
    return status
#}}}
def get_total_seconds(td): #{{{
    """
    return the total_seconds for the timedate.timedelta object
    for python version >2.7 this is not needed
    """
    return (td.microseconds + (td.seconds + td.days * 24 * 3600) * 1e6) / 1e6
#}}}
def GetNumSuqJob(node):#{{{
    # get the number of queueing jobs on the node
    # return -1 if the url is not accessible
    url = "http://%s/cgi-bin/get_suqlist.cgi?base=log"%(node)
    try:
        rtValue = requests.get(url, timeout=2)
        if rtValue.status_code < 400:
            lines = rtValue.content.split("\n")
            cnt_queue_job = 0
            for line in lines:
                strs = line.split()
                if len(strs)>=4 and strs[0].isdigit():
                    status = strs[2]
                    if status == "Wait":
                        cnt_queue_job += 1
            return cnt_queue_job
        else:
            return -1
    except:
        myfunc.WriteFile("requests.get(%s) failed\n"%(url), gen_errfile, "a", True)
        return -1

#}}}
def IsHaveAvailNode(cntSubmitJobDict):#{{{
    for node in cntSubmitJobDict:
        [num_queue_job, max_allowed_job] = cntSubmitJobDict[node]
        if num_queue_job < max_allowed_job:
            return True
    return False
#}}}
def GetNumSeqSameUserDict(joblist):#{{{
# calculate the number of sequences for each user in the queue or running
# Fixed error for getting numseq at 2015-04-11
    numseq_user_dict = {}
    for i in xrange(len(joblist)):
        li1 = joblist[i]
        jobid1 = li1[0]
        ip1 = li1[3]
        email1 = li1[4]
        try:
            numseq1 = int(li1[5])
        except:
            numseq1 = 123
            pass
        if not jobid1 in numseq_user_dict:
            numseq_user_dict[jobid1] = 0
        numseq_user_dict[jobid1] += numseq1
        if ip1 == "" and email1 == "":
            continue

        for j in xrange(len(joblist)):
            li2 = joblist[j]
            if i == j:
                continue

            jobid2 = li2[0]
            ip2 = li2[3]
            email2 = li2[4]
            try:
                numseq2 = int(li2[5])
            except:
                numseq2 = 123
                pass
            if ((ip2 != "" and ip2 == ip1) or
                    (email2 != "" and email2 == email1)):
                numseq_user_dict[jobid1] += numseq2
    return numseq_user_dict
#}}}
def CreateRunJoblog(path_result, submitjoblogfile, runjoblogfile,#{{{
        finishedjoblogfile, loop):
    myfunc.WriteFile("CreateRunJoblog...\n", gen_logfile, "a", True)
    # Read entries from submitjoblogfile, checking in the result folder and
    # generate two logfiles: 
    #   1. runjoblogfile 
    #   2. finishedjoblogfile
    # when loop == 0, for unfinished jobs, re-generate finished_seqs.txt
    hdl = myfunc.ReadLineByBlock(submitjoblogfile)
    if hdl.failure:
        return 1

    finished_jobid_list = []
    if os.path.exists(finishedjoblogfile):
        finished_job_dict = myfunc.ReadFinishedJobLog(finishedjoblogfile)

    new_finished_list = []  # Finished or Failed
    new_runjob_list = []    # Running
    new_waitjob_list = []    # Queued
    lines = hdl.readlines()
    while lines != None:
        for line in lines:
            strs = line.split("\t")
            if len(strs) < 8:
                continue
            submit_date_str = strs[0]
            jobid = strs[1]
            ip = strs[2]
            numseq_str = strs[3]
            jobname = strs[5]
            email = strs[6].strip()
            method_submission = strs[7]
            start_date_str = ""
            finish_date_str = ""
            rstdir = "%s/%s"%(path_result, jobid)

            numseq = 1
            try:
                numseq = int(numseq_str)
            except:
                pass

            if jobid in finished_job_dict:
                if os.path.exists(rstdir):
                    li = [jobid] + finished_job_dict[jobid]
                    new_finished_list.append(li)
                continue


            status = get_job_status(jobid)

            starttagfile = "%s/%s"%(rstdir, "runjob.start")
            finishtagfile = "%s/%s"%(rstdir, "runjob.finish")
            if os.path.exists(starttagfile):
                start_date_str = myfunc.ReadFile(starttagfile).strip()
            if os.path.exists(finishtagfile):
                finish_date_str = myfunc.ReadFile(finishtagfile).strip()

            li = [jobid, status, jobname, ip, email, numseq_str,
                    method_submission, submit_date_str, start_date_str,
                    finish_date_str]
            if status in ["Finished", "Failed"]:
                new_finished_list.append(li)

            # single-sequence job submitted from the web-page will be
            # submmitted by suq
            if numseq > 1 or method_submission == "wsdl":
                if status == "Running":
                    new_runjob_list.append(li)
                elif status == "Wait":
                    new_waitjob_list.append(li)
        lines = hdl.readlines()
    hdl.close()

# re-write logs of finished jobs
    li_str = []
    for li in new_finished_list:
        li_str.append("\t".join(li))
    if len(li_str)>0:
        myfunc.WriteFile("\n".join(li_str)+"\n", finishedjoblogfile, "w", True)
    else:
        myfunc.WriteFile("", finishedjoblogfile, "w", True)
# re-write logs of finished jobs for each IP
    new_finished_dict = {}
    for li in new_finished_list:
        ip = li[3]
        if not ip in new_finished_dict:
            new_finished_dict[ip] = []
        new_finished_dict[ip].append(li)
    for ip in new_finished_dict:
        finished_list_for_this_ip = new_finished_dict[ip]
        divide_finishedjoblogfile = "%s/divided/%s_finished_job.log"%(path_log,
                ip)
        li_str = []
        for li in finished_list_for_this_ip:
            li_str.append("\t".join(li))
        if len(li_str)>0:
            myfunc.WriteFile("\n".join(li_str)+"\n", divide_finishedjoblogfile, "w", True)
        else:
            myfunc.WriteFile("", divide_finishedjoblogfile, "w", True)

# write logs of running and queuing jobs
# the queuing jobs are sorted in descending order by the suq priority
# frist get numseq_this_user for each jobs
# format of numseq_this_user: {'jobid': numseq_this_user}
    numseq_user_dict = GetNumSeqSameUserDict(new_runjob_list + new_waitjob_list)

# now append numseq_this_user and priority score to new_waitjob_list and
# new_runjob_list

    for joblist in [new_waitjob_list, new_runjob_list]:
        for li in joblist:
            jobid = li[0]
            ip = li[3]
            email = li[4].strip()

            # if loop == 0 , for new_waitjob_list and new_runjob_list
            # re-generate finished_seqs.txt
            if loop == 0:#{{{
                rstdir = "%s/%s"%(path_result, jobid)
                outpath_result = "%s/%s"%(rstdir, jobid)
                finished_seq_file = "%s/finished_seqs.txt"%(outpath_result)
                finished_seqs_idlist = myfunc.ReadIDList2(finished_seq_file, col=0, delim="\t")
                finished_seqs_idset = set(finished_seqs_idlist)
                finished_info_list = []
                queryfile = "%s/query.fa"%(rstdir)
                (seqidlist, seqannolist, seqlist) = myfunc.ReadFasta(queryfile)
                try:
                    dirlist = os.listdir(outpath_result)
                    for dd in dirlist:
                        if dd.find("seq_") == 0 and dd not in finished_seqs_idset:
                            origIndex = int(dd.split("_")[1])
                            outpath_this_seq = "%s/%s"%(outpath_result, dd)
                            timefile = "%s/time.txt"%(outpath_this_seq)
                            runtime1 = 0.0
                            if os.path.exists(timefile):
                                txt = myfunc.ReadFile(timefile).strip()
                                ss2 = txt.split(";")
                                try:
                                    runtime = float(ss2[1])
                                except:
                                    runtime = runtime1
                                    pass
                            else:
                                runtime = runtime1

                            topfile = "%s/%s/topcons.top"%(
                                    outpath_this_seq, "Topcons")
# get origIndex and then read description the description list
                            try:
                                description = seqannolist[origIndex]
                            except:
                                description = "seq_%d"%(origIndex)
                            top = myfunc.ReadFile(topfile).strip()
                            numTM = myfunc.CountTM(top)
                            posSP = myfunc.GetSPPosition(top)
                            if len(posSP) > 0:
                                isHasSP = True
                            else:
                                isHasSP = False
                            info_finish = [ dd, str(len(top)), str(numTM),
                                    str(isHasSP), "newrun", str(runtime), description]
                            finished_info_list.append("\t".join(info_finish))
                except:
                    myfunc.WriteFile("Failed to os.listdir(%s)\n"%(outpath_result), gen_errfile, "a", True)
                    pass
                if len(finished_info_list)>0:
                    myfunc.WriteFile("\n".join(finished_info_list)+"\n", finished_seq_file, "a", True)
            #}}}

            try:
                numseq = int(li[5])
            except:
                numseq = 1
                pass
            try:
                numseq_this_user = numseq_user_dict[jobid]
            except:
                numseq_this_user = numseq
                pass
            priority = myfunc.GetSuqPriority(numseq_this_user)

            if email in vip_user_list:
                numseq_this_user = 1
                priority = 999999999.0
                myfunc.WriteFile("email %s in vip_user_list\n"%(email), gen_logfile, "a", True)

            li.append(numseq_this_user)
            li.append(priority)


    # sort the new_waitjob_list in descending order by priority
    new_waitjob_list = sorted(new_waitjob_list, key=lambda x:x[11], reverse=True)
    new_runjob_list = sorted(new_runjob_list, key=lambda x:x[11], reverse=True)

    # write to runjoblogfile
    li_str = []
    for joblist in [new_waitjob_list, new_runjob_list]:
        for li in joblist:
            li2 = li[:10]+[str(li[10]), str(li[11])]
            li_str.append("\t".join(li2))
#     print "write to", runjoblogfile
#     print "\n".join(li_str)
    if len(li_str)>0:
        myfunc.WriteFile("\n".join(li_str)+"\n", runjoblogfile, "w", True)
    else:
        myfunc.WriteFile("", runjoblogfile, "w", True)

#}}}
def SubmitJob(jobid,cntSubmitJobDict, numseq_this_user):#{{{
# for each job rstdir, keep three log files, 
# 1.seqs finished, finished_seq log keeps all information, finished_index_log
#   can be very compact to speed up reading, e.g.
#   1-5 7-9 etc
# 2.seqs queued remotely , format:
#       index node remote_jobid
# 3. format of the torun_idx_file
#    origIndex

    rmsg = ""
    myfunc.WriteFile("SubmitJob for %s, numseq_this_user=%d\n"%(jobid,
        numseq_this_user), gen_logfile, "a", True)
    rstdir = "%s/%s"%(path_result, jobid)
    outpath_result = "%s/%s"%(rstdir, jobid)
    if not os.path.exists(outpath_result):
        os.mkdir(outpath_result)

    finished_idx_file = "%s/finished_seqindex.txt"%(rstdir)
    failed_idx_file = "%s/failed_seqindex.txt"%(rstdir)
    remotequeue_idx_file = "%s/remotequeue_seqindex.txt"%(rstdir)
    torun_idx_file = "%s/torun_seqindex.txt"%(rstdir) # ordered seq index to run
    cnttry_idx_file = "%s/cntsubmittry_seqindex.txt"%(rstdir)#index file to keep log of tries

    errfile = "%s/%s"%(rstdir, "runjob.err")
    finished_seq_file = "%s/finished_seqs.txt"%(outpath_result)
    tmpdir = "%s/tmpdir"%(rstdir)
    qdinittagfile = "%s/runjob.qdinit"%(rstdir)
    failedtagfile = "%s/%s"%(rstdir, "runjob.failed")
    starttagfile = "%s/%s"%(rstdir, "runjob.start")
    fafile = "%s/query.fa"%(rstdir)
    split_seq_dir = "%s/splitaa"%(tmpdir)
    forceruntagfile = "%s/forcerun"%(rstdir)

    finished_idx_list = []
    failed_idx_list = []    # [origIndex]
    if os.path.exists(finished_idx_file):
        finished_idx_list = list(set(myfunc.ReadIDList(finished_idx_file)))
    if os.path.exists(failed_idx_file):
        failed_idx_list = list(set(myfunc.ReadIDList(failed_idx_file)))

    processed_idx_set = set(finished_idx_list) | set(failed_idx_list)

    jobinfofile = "%s/jobinfo"%(rstdir)
    jobinfo = ""
    if os.path.exists(jobinfofile):
        jobinfo = myfunc.ReadFile(jobinfofile).strip()
    jobinfolist = jobinfo.split("\t")
    email = ""
    if len(jobinfolist) >= 8:
        email = jobinfolist[6]
        method_submission = jobinfolist[7]

    # the first time when the this jobid is processed, do the following
    # 1. first get cached results, write runjob.start tagfile if cache result is
    #    available
    # 2. run Scampi prediction to estimate the number of TMs and then calculate the
    #    running order
    # 3. generate a file with sorted seqindex
    # 4. generate splitted sequence files named by the original seqindex
    if not os.path.exists(qdinittagfile): #initialization#{{{
        if not os.path.exists(tmpdir):
            os.mkdir(tmpdir)

        init_finished_idx_list = [] # [origIndex]
        # ==== 1.dealing with cached results 
        (seqIDList, seqAnnoList, seqList) = myfunc.ReadFasta(fafile)
        if len(seqIDList) <= 0:
            date_str = time.strftime("%Y-%m-%d %H:%M:%S")
            myfunc.WriteFile(date_str, failedtagfile, "w", True)
            myfunc.WriteFile("Read query seq file failed. Zero sequence read in.\n", errfile, "a", True)
            return 1
        toRunDict = {}
        if os.path.exists(forceruntagfile):
            for i in xrange(len(seqIDList)):
                toRunDict[i] = [seqList[i], 0, seqAnnoList[i]]
        else:
            for i in xrange(len(seqIDList)):
                isSkip = False
                outpath_this_seq = "%s/%s"%(outpath_result, "seq_%d"%i)
                subfoldername_this_seq = "seq_%d"%(i)
                md5_key = hashlib.md5(seqList[i]).hexdigest()
                subfoldername = md5_key[:2]
                md5_link = "%s/%s/%s"%(path_md5cache, subfoldername, md5_key)
                if os.path.exists(md5_link):
                    # create a symlink to the cache
                    rela_path = os.path.relpath(md5_link, outpath_result) #relative path
                    os.chdir(outpath_result)
                    if not os.path.exists(subfoldername_this_seq):
                        os.symlink(rela_path, subfoldername_this_seq)
                    if os.path.exists(outpath_this_seq):

                        if not os.path.exists(starttagfile): #write start tagfile
                            date_str = time.strftime("%Y-%m-%d %H:%M:%S")
                            myfunc.WriteFile(date_str, starttagfile, "w", True)

                        runtime = 0.0 #in seconds
                        topfile = "%s/%s/topcons.top"%(
                                outpath_this_seq, "Topcons")
                        top = myfunc.ReadFile(topfile).strip()
                        numTM = myfunc.CountTM(top)
                        posSP = myfunc.GetSPPosition(top)
                        if len(posSP) > 0:
                            isHasSP = True
                        else:
                            isHasSP = False
                        info_finish = [ "seq_%d"%i,
                                str(len(seqList[i])), str(numTM),
                                str(isHasSP), "cached", str(runtime),
                                seqAnnoList[i]]
                        myfunc.WriteFile("\t".join(info_finish)+"\n",
                                finished_seq_file, "a", isFlush=True)
                        init_finished_idx_list.append(str(i))
                        isSkip = True

                if not isSkip:
                    # first try to delete the outfolder if exists
                    if os.path.exists(outpath_this_seq):
                        try:
                            shutil.rmtree(outpath_this_seq)
                        except OSError:
                            pass
                    toRunDict[i] = [seqList[i], 0, seqAnnoList[i]] #init value for numTM is 0

        #Write finished_idx_file
        if len(init_finished_idx_list)>0:
            myfunc.WriteFile("\n".join(init_finished_idx_list)+"\n", finished_idx_file, "a", True)

        # run scampi single to estimate the number of TM helices and then run
        # the query sequences in the descending order of numTM
        torun_all_seqfile = "%s/%s"%(tmpdir, "query.torun.fa")
        dumplist = []
        for key in toRunDict:
            top = toRunDict[key][0]
            dumplist.append(">%s\n%s"%(str(key), top))
        if len(dumplist)>0:
            myfunc.WriteFile("\n".join(dumplist)+"\n", torun_all_seqfile, "w", True)
        else:
            myfunc.WriteFile("", torun_all_seqfile, "w", True)
        del dumplist

        topfile_scampiseq = "%s/%s"%(tmpdir, "query.torun.fa.topo")
        if os.path.exists(torun_all_seqfile):
            # run scampi to estimate the number of TM helices
            cmd = [script_scampi, torun_all_seqfile, "-outpath", tmpdir]
            cmdline = " ".join(cmd)
            try:
                rmsg = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
            except subprocess.CalledProcessError, e:
                date_str = time.strftime("%Y-%m-%d %H:%M:%S")
                myfunc.WriteFile("[Date: %s]"%(date_str)+str(e)+"\n", gen_errfile, "a", True)
                myfunc.WriteFile("cmdline = %s\n"%(cmdline), gen_errfile, "a", True)
                pass
        if os.path.exists(topfile_scampiseq):
            (idlist_scampi, annolist_scampi, toplist_scampi) = myfunc.ReadFasta(topfile_scampiseq)
            for jj in xrange(len(idlist_scampi)):
                numTM = myfunc.CountTM(toplist_scampi[jj])
                try:
                    toRunDict[int(idlist_scampi[jj])][1] = numTM
                except (KeyError, ValueError, TypeError):
                    pass

        sortedlist = sorted(toRunDict.items(), key=lambda x:x[1][1], reverse=True)

        # Write splitted fasta file and write a torunlist.txt
        if not os.path.exists(split_seq_dir):
            os.mkdir(split_seq_dir)

        torun_index_str_list = [str(x[0]) for x in sortedlist]
        if len(torun_index_str_list)>0:
            myfunc.WriteFile("\n".join(torun_index_str_list)+"\n", torun_idx_file, "w", True)
        else:
            myfunc.WriteFile("", torun_idx_file, "w", True)

        # write cnttry file for each jobs to run
        cntTryDict = {}
        for idx in torun_index_str_list:
            cntTryDict[int(idx)] = 0
        json.dump(cntTryDict, open(cnttry_idx_file, "w"))

        for item in sortedlist:
            origIndex = item[0]
            seq = item[1][0]
            description = item[1][2]
            seqfile_this_seq = "%s/%s"%(split_seq_dir, "query_%d.fa"%(origIndex))
            seqcontent = ">%s\n%s\n"%(description, seq)
            myfunc.WriteFile(seqcontent, seqfile_this_seq, "w", True)
        # qdinit file is written at the end of initialization, to make sure
        # that initialization is either not started or completed
        date_str = time.strftime("%Y-%m-%d %H:%M:%S")
        myfunc.WriteFile(date_str, qdinittagfile, "w", True)
#}}}


    # 5. try to submit the job 
    if os.path.exists(forceruntagfile):
        isforcerun = "True"
    else:
        isforcerun = "False"
    toRunIndexList = [] # index in str
    processedIndexSet = set([]) #seq index set that are already processed
    submitted_loginfo_list = []
    if os.path.exists(torun_idx_file):
        toRunIndexList = myfunc.ReadIDList(torun_idx_file)
        # unique the list but keep the order
        toRunIndexList = myfunc.uniquelist(toRunIndexList)
    if len(toRunIndexList) > 0:
        iToRun = 0
        numToRun = len(toRunIndexList)
        for node in cntSubmitJobDict:
            if iToRun >= numToRun:
                break
            wsdl_url = "http://%s/pred/api_submitseq/?wsdl"%(node)
            try:
                myclient = Client(wsdl_url, cache=None, timeout=30)
            except:
                date_str = time.strftime("%Y-%m-%d %H:%M:%S")
                myfunc.WriteFile("[Date: %s] Failed to access %s\n"%(date_str, wsdl_url), gen_errfile, "a", True)
                break

            [cnt, maxnum] = cntSubmitJobDict[node]
            MAX_SUBMIT_TRY = 3
            cnttry = 0
            while cnt < maxnum and iToRun < numToRun:
                origIndex = int(toRunIndexList[iToRun])
                seqfile_this_seq = "%s/%s"%(split_seq_dir, "query_%d.fa"%(origIndex))
                # ignore already existing query seq, this is an ugly solution,
                # the generation of torunindexlist has a bug
                outpath_this_seq = "%s/%s"%(outpath_result, "seq_%d"%origIndex)
                if os.path.exists(outpath_this_seq):
                    iToRun += 1
                    continue


                if DEBUG:
                    myfunc.WriteFile("DEBUG: cnt (%d) < maxnum (%d) "\
                            "and iToRun(%d) < numToRun(%d)"%(cnt, maxnum, iToRun, numToRun), gen_logfile, "a", True)
                fastaseq = ""
                seqid = ""
                seqanno = ""
                seq = ""
                if not os.path.exists(seqfile_this_seq):
                    all_seqfile = "%s/query.fa"%(rstdir)
                    try:
                        (allseqidlist, allannolist, allseqlist) = myfunc.ReadFasta(all_seqfile)
                        seqid = allseqidlist[origIndex]
                        seqanno = allannolist[origIndex]
                        seq = allseqlist[origIndex]
                        fastaseq = ">%s\n%s\n"%(seqanno, seq)
                    except:
                        pass
                else:
                    fastaseq = myfunc.ReadFile(seqfile_this_seq)#seq text in fasta format
                    (seqid, seqanno, seq) = myfunc.ReadSingleFasta(seqfile_this_seq)


                isSubmitSuccess = False
                if len(seq) > 0:
                    fixtop = ""
                    jobname = ""
                    if not email in vip_user_list:
                        useemail = ""
                    else:
                        useemail = email
                    try:
                        rtValue = myclient.service.submitjob_remote(fastaseq, fixtop,
                                jobname, useemail, str(numseq_this_user), isforcerun)
                    except:
                        date_str = time.strftime("%Y-%m-%d %H:%M:%S")
                        myfunc.WriteFile("[Date: %s] Failed to run myclient.service.submitjob_remote\n"%(date_str), gen_errfile, "a", True)
                        rtValue = []
                        pass

                    cnttry += 1
                    if len(rtValue) >= 1:
                        strs = rtValue[0]
                        if len(strs) >=5:
                            remote_jobid = strs[0]
                            result_url = strs[1]
                            numseq_str = strs[2]
                            errinfo = strs[3]
                            warninfo = strs[4]
                            if remote_jobid != "None" and remote_jobid != "":
                                isSubmitSuccess = True
                                epochtime = time.time()
                                # 6 fields in the file remotequeue_idx_file
                                txt =  "%d\t%s\t%s\t%s\t%s\t%f"%( origIndex,
                                        node, remote_jobid, seqanno, seq,
                                        epochtime)
                                submitted_loginfo_list.append(txt)
                                cnttry = 0  #reset cnttry to zero
                        else:
                            myfunc.WriteFile("bad wsdl return value\n", gen_errfile, "a", True)
                if isSubmitSuccess:
                    cnt += 1
                if isSubmitSuccess or cnttry >= MAX_SUBMIT_TRY:
                    iToRun += 1
                    processedIndexSet.add(str(origIndex))
                    if DEBUG:
                        myfunc.WriteFile("DEBUG: jobid %s processedIndexSet.add(str(%d))\n"%(jobid, origIndex), gen_logfile, "a", True)
            # update cntSubmitJobDict for this node
            cntSubmitJobDict[node] = [cnt, maxnum]

    # finally, append submitted_loginfo_list to remotequeue_idx_file 
    if len(submitted_loginfo_list)>0:
        myfunc.WriteFile("\n".join(submitted_loginfo_list)+"\n", remotequeue_idx_file, "a", True)
    # update torun_idx_file
    newToRunIndexList = []
    for idx in toRunIndexList:
        if not idx in processedIndexSet:
            newToRunIndexList.append(idx)
    if DEBUG:
        myfunc.WriteFile("DEBUG: jobid %s, newToRunIndexList="%(jobid) + " ".join( newToRunIndexList)+"\n", gen_logfile, "a", True)

    if len(newToRunIndexList)>0:
        myfunc.WriteFile("\n".join(newToRunIndexList)+"\n", torun_idx_file, "w", True)
    else:
        myfunc.WriteFile("", torun_idx_file, "w", True)

    return 0
#}}}
def GetResult(jobid):#{{{
    # retrieving result from the remote server for this job
    myfunc.WriteFile("GetResult for %s.\n" %(jobid), gen_logfile, "a", True)
    MAX_RESUBMIT = 2
    rstdir = "%s/%s"%(path_result, jobid)
    outpath_result = "%s/%s"%(rstdir, jobid)
    if not os.path.exists(outpath_result):
        os.mkdir(outpath_result)

    remotequeue_idx_file = "%s/remotequeue_seqindex.txt"%(rstdir)

    torun_idx_file = "%s/torun_seqindex.txt"%(rstdir) # ordered seq index to run
    finished_idx_file = "%s/finished_seqindex.txt"%(rstdir)
    failed_idx_file = "%s/failed_seqindex.txt"%(rstdir)

    starttagfile = "%s/%s"%(rstdir, "runjob.start")
    cnttry_idx_file = "%s/cntsubmittry_seqindex.txt"%(rstdir)#index file to keep log of tries
    tmpdir = "%s/tmpdir"%(rstdir)
    finished_seq_file = "%s/finished_seqs.txt"%(outpath_result)

    finished_info_list = [] #[info for finished record]
    finished_idx_list = [] # [origIndex]
    failed_idx_list = []    # [origIndex]
    resubmit_idx_list = []  # [origIndex]
    keep_queueline_list = [] # [line] still in queue

    cntTryDict = {}
    if os.path.exists(cnttry_idx_file):
        with open(cnttry_idx_file, 'r') as fpin:
            cntTryDict = json.load(fpin)

    text = ""
    if os.path.exists(remotequeue_idx_file):
        text = myfunc.ReadFile(remotequeue_idx_file)
    if text == "":
        return 1
    lines = text.split("\n")

    nodeSet = set([])
    for i in xrange(len(lines)):
        line = lines[i]
        if not line or line[0] == "#":
            continue
        strs = line.split("\t")
        if len(strs) != 6:
            continue
        node = strs[1]
        nodeSet.add(node)

    myclientDict = {}
    for node in nodeSet:
        wsdl_url = "http://%s/pred/api_submitseq/?wsdl"%(node)
        try:
            myclient = Client(wsdl_url, cache=None, timeout=30)
            myclientDict[node] = myclient
        except:
            date_str = time.strftime("%Y-%m-%d %H:%M:%S")
            myfunc.WriteFile("[Date: %s] Failed to access %s\n"%(date_str, wsdl_url), gen_errfile, "a", True)
            pass


    for i in xrange(len(lines)):#{{{
        line = lines[i]

        if DEBUG:
            myfunc.WriteFile("Process %s\n"%(line), gen_logfile, "a", True)
        if not line or line[0] == "#":
            continue
        strs = line.split("\t")
        if len(strs) != 6:
            continue
        origIndex = int(strs[0])
        node = strs[1]
        remote_jobid = strs[2]
        description = strs[3]
        seq = strs[4]
        submit_time_epoch = float(strs[5])
        outpath_this_seq = "%s/%s"%(outpath_result, "seq_%d"%origIndex)

        try:
            myclient = myclientDict[node]
        except KeyError:
            continue
        try:
            rtValue = myclient.service.checkjob(remote_jobid)
        except:
            date_str = time.strftime("%Y-%m-%d %H:%M:%S")
            myfunc.WriteFile("[Date: %s] Failed to run myclient.service.checkjob(%s)\n"%(date_str, remote_jobid), gen_errfile, "a", True)
            rtValue = []
            pass
        isSuccess = False
        isFinish_remote = False
        if len(rtValue) >= 1:
            ss2 = rtValue[0]
            if len(ss2)>=3:
                status = ss2[0]
                result_url = ss2[1]
                errinfo = ss2[2]

                if errinfo and errinfo.find("does not exist")!=-1:
                    isFinish_remote = True

                if status == "Finished":#{{{
                    isFinish_remote = True
                    outfile_zip = "%s/%s.zip"%(tmpdir, remote_jobid)
                    isRetrieveSuccess = False
                    if myfunc.IsURLExist(result_url,timeout=2):
                        try:
                            urllib.urlretrieve (result_url, outfile_zip)
                            isRetrieveSuccess = True
                        except:
                            pass
                    if os.path.exists(outfile_zip) and isRetrieveSuccess:
                        cmd = ["unzip", outfile_zip, "-d", tmpdir]
                        cmdline = " ".join(cmd)
                        try:
                            rmsg = subprocess.check_output(cmd)
                        except subprocess.CalledProcessError, e:
                            date_str = time.strftime("%Y-%m-%d %H:%M:%S")
                            myfunc.WriteFile("[%s] cmdline=%s\nerrmsg=%s\n"%(
                                    date_str, cmdline, str(e)), gen_errfile, "a", True)
                            pass
                        rst_this_seq = "%s/%s/seq_0"%(tmpdir, remote_jobid)
                        if os.path.exists(outpath_this_seq):
                            shutil.rmtree(outpath_this_seq)
                        if os.path.exists(rst_this_seq) and not os.path.exists(outpath_this_seq):
                            cmd = ["mv","-f", rst_this_seq, outpath_this_seq]
                            cmdline = " ".join(cmd)
                            try:
                                rmsg = subprocess.check_output(cmd)
                            except subprocess.CalledProcessError, e:
                                date_str = time.strftime("%Y-%m-%d %H:%M:%S")
                                myfunc.WriteFile( "[%s] cmdline=%s\nerrmsg=%s\n"%(
                                        date_str, cmdline, str(e)), gen_errfile, "a", True)
                                pass
                            if os.path.exists(outpath_this_seq):
                                isSuccess = True

                            if isSuccess:
                                # delete the data on the remote server
                                try:
                                    rtValue2 = myclient.service.deletejob(remote_jobid)
                                except:
                                    date_str = time.strftime("%Y-%m-%d %H:%M:%S")
                                    myfunc.WriteFile( "[%s] Failed to run myclient.service.deletejob(%s)\n"%(date_str, remote_jobid), gen_errfile, "a", True)
                                    rtValue2 = []
                                    pass

                                logmsg = ""
                                if len(rtValue2) >= 1:
                                    ss2 = rtValue2[0]
                                    if len(ss2) >= 2:
                                        status = ss2[0]
                                        errmsg = ss2[1]
                                        if status == "Succeeded":
                                            logmsg = "Successfully deleted data on %s "\
                                                    "for %s"%(node, remote_jobid)
                                        else:
                                            logmsg = "Failed to delete data on %s for "\
                                                    "%s\nError message:\n%s\n"%(node, remote_jobid, errmsg)
                                else:
                                    logmsg = "Failed to call deletejob %s via WSDL on %s\n"%(remote_jobid, node)

                                # delete the zip file
                                os.remove(outfile_zip)
                                shutil.rmtree("%s/%s"%(tmpdir, remote_jobid))

                                # create or update the md5 cache
                                md5_key = hashlib.md5(seq).hexdigest()
                                subfoldername = md5_key[:2]
                                md5_subfolder = "%s/%s"%(path_md5cache, subfoldername)
                                md5_link = "%s/%s/%s"%(path_md5cache, subfoldername, md5_key)
                                if os.path.exists(md5_link):
                                    try:
                                        os.unlink(md5_link)
                                    except:
                                        pass
                                subfolder_md5 = "%s/%s"%(path_md5cache, subfoldername)
                                if not os.path.exists(subfolder_md5):
                                    try:
                                        os.makedirs(subfolder_md5)
                                    except:
                                        pass

                                rela_path = os.path.relpath(outpath_this_seq, md5_subfolder) #relative path
                                try:
                                    os.chdir(md5_subfolder)
                                    os.symlink(rela_path,  md5_key)
                                except:
                                    pass


#}}}
                elif status in ["Failed", "None"]:
                    # the job is failed for this sequence, try to re-submit
                    isFinish_remote = True
                    cnttry = 1
                    try:
                        cnttry = cntTryDict[origIndex]
                    except KeyError:
                        cnttry = 1
                        pass
                    if cnttry < MAX_RESUBMIT:
                        resubmit_idx_list.append(str(origIndex))
                        cntTryDict[origIndex] = cnttry+1
                    else:
                        failed_idx_list.append(str(origIndex))
                if status != "Wait" and not os.path.exists(starttagfile):
                    date_str = time.strftime("%Y-%m-%d %H:%M:%S")
                    myfunc.WriteFile(date_str, starttagfile, "w", True)
        if isSuccess:#{{{
            time_now = time.time()
            runtime = 5.0
            runtime1 = time_now - submit_time_epoch #in seconds
            timefile = "%s/time.txt"%(outpath_this_seq)
            if os.path.exists(timefile):
                txt = myfunc.ReadFile(timefile).strip()
                ss2 = txt.split(";")
                try:
                    runtime = float(ss2[1])
                except:
                    runtime = runtime1
                    pass
            else:
                runtime = runtime1

            topfile = "%s/%s/topcons.top"%(
                    outpath_this_seq, "Topcons")
            top = myfunc.ReadFile(topfile).strip()
            numTM = myfunc.CountTM(top)
            posSP = myfunc.GetSPPosition(top)
            if len(posSP) > 0:
                isHasSP = True
            else:
                isHasSP = False
            info_finish = [ "seq_%d"%origIndex, str(len(seq)), str(numTM),
                    str(isHasSP), "newrun", str(runtime), description]
            finished_info_list.append("\t".join(info_finish))
            finished_idx_list.append(str(origIndex))#}}}

        if not isFinish_remote:
            keep_queueline_list.append(line)
#}}}
    #Finally, write log files
    finished_idx_list = list(set(finished_idx_list))
    failed_idx_list = list(set(failed_idx_list))
    resubmit_idx_list = list(set(resubmit_idx_list))


    if len(finished_info_list)>0:
        myfunc.WriteFile("\n".join(finished_info_list)+"\n", finished_seq_file, "a", True)
    if len(finished_idx_list)>0:
        myfunc.WriteFile("\n".join(finished_idx_list)+"\n", finished_idx_file, "a", True)
    if len(failed_idx_list)>0:
        myfunc.WriteFile("\n".join(failed_idx_list)+"\n", failed_idx_file, "a", True)
    if len(resubmit_idx_list)>0:
        myfunc.WriteFile("\n".join(resubmit_idx_list)+"\n", torun_idx_file, "a", True)

    if len(keep_queueline_list)>0:
        myfunc.WriteFile("\n".join(keep_queueline_list)+"\n", remotequeue_idx_file, "w", True);
    else:
        myfunc.WriteFile("", remotequeue_idx_file, "w", True);

    # in case of missing queries, if remotequeue_idx_file is empty and
    # torun_idx_file is empty but still not finished, force add to
    # torun_idx_file
    if os.path.getsize(remotequeue_idx_file)<1 and os.path.getsize(torun_idx_file)<1:
        completed_idx_set = set(myfunc.ReadIDList(finished_idx_file) + 
                myfunc.ReadIDList(failed_idx_file))
        jobinfofile = "%s/jobinfo"%(rstdir)
        jobinfo = myfunc.ReadFile(jobinfofile).strip()
        jobinfolist = jobinfo.split("\t")
        if len(jobinfolist) >= 8:
            numseq = int(jobinfolist[3])

        if len(completed_idx_set) < numseq:
            torun_idx_list = list(set(range(numseq))-completed_idx_set)
            torun_idx_str_list = [str(x) for x in torun_idx_list]
            for idx in torun_idx_list:
                try:
                    cntTryDict[idx] += 1
                except:
                    cntTryDict[idx] = 1
                    pass
            myfunc.WriteFile("\n".join(torun_idx_str_list)+"\n", torun_idx_file, "a", True)


    with open(cnttry_idx_file, 'w') as fpout:
        json.dump(cntTryDict, fpout)



    return 0
#}}}

def CheckIfJobFinished(jobid, numseq, email):#{{{
    # check if the job is finished and write tagfiles
    myfunc.WriteFile("CheckIfJobFinished for %s.\n" %(jobid), gen_logfile, "a", True)
    rstdir = "%s/%s"%(path_result, jobid)
    tmpdir = "%s/tmpdir"%(rstdir)
    outpath_result = "%s/%s"%(rstdir, jobid)
    errfile = "%s/%s"%(rstdir, "runjob.err")
    logfile = "%s/%s"%(rstdir, "runjob.log")
    finished_idx_file = "%s/finished_seqindex.txt"%(rstdir)
    failed_idx_file = "%s/failed_seqindex.txt"%(rstdir)
    seqfile = "%s/query.fa"%(rstdir)

    base_www_url_file = "%s/static/log/base_www_url.txt"%(basedir)
    base_www_url = ""

    finished_idx_list = []
    failed_idx_list = []
    if os.path.exists(finished_idx_file):
        finished_idx_list = myfunc.ReadIDList(finished_idx_file)
        finished_idx_list = list(set(finished_idx_list))
    if os.path.exists(failed_idx_file):
        failed_idx_list = myfunc.ReadIDList(failed_idx_file)
        failed_idx_list = list(set(failed_idx_list))

    finishtagfile = "%s/%s"%(rstdir, "runjob.finish")
    failedtagfile = "%s/%s"%(rstdir, "runjob.failed")
    starttagfile = "%s/%s"%(rstdir, "runjob.start")

    num_processed = len(finished_idx_list)+len(failed_idx_list)
    finish_status = "" #["success", "failed", "partly_failed"]
    if num_processed >= numseq:# finished
        if len(failed_idx_list) == 0:
            finish_status = "success"
        elif len(failed_idx_list) >= numseq:
            finish_status = "failed"
        else:
            finish_status = "partly_failed"

        if os.path.exists(base_www_url_file):
            base_www_url = myfunc.ReadFile(base_www_url_file).strip()
        if base_www_url == "":
            base_www_url = "http://topcons.net"

        date_str = time.strftime("%Y-%m-%d %H:%M:%S")
        date_str_epoch = time.time()
        myfunc.WriteFile(date_str, finishtagfile, "w", True)

        # Now write the text output to a single file
        statfile = "%s/%s"%(outpath_result, "stat.txt")
        resultfile_text = "%s/%s"%(outpath_result, "query.result.txt")
        (seqIDList, seqAnnoList, seqList) = myfunc.ReadFasta(seqfile)
        maplist = []
        for i in xrange(len(seqIDList)):
            maplist.append("%s\t%d\t%s\t%s"%("seq_%d"%i, len(seqList[i]),
                seqAnnoList[i], seqList[i]))
        start_date_str = myfunc.ReadFile(starttagfile).strip()
        start_date_epoch = datetime.strptime(start_date_str, "%Y-%m-%d %H:%M:%S").strftime('%s')
        all_runtime_in_sec = float(date_str_epoch) - float(start_date_epoch)

        myfunc.WriteTOPCONSTextResultFile(resultfile_text, outpath_result, maplist,
                all_runtime_in_sec, base_www_url, statfile=statfile)

        # now making zip instead (for windows users)
        # note that zip rq will zip the real data for symbolic links
        zipfile = "%s.zip"%(jobid)
        zipfile_fullpath = "%s/%s"%(rstdir, zipfile)
        os.chdir(rstdir)
        cmd = ["zip", "-rq", zipfile, jobid]
        try:
            subprocess.check_output(cmd)
        except subprocess.CalledProcessError, e:
            myfunc.WriteFile(str(e)+"\n", errfile, "a", True)
            pass

        if len(failed_idx_list)>0:
            myfunc.WriteFile(date_str, failedtagfile, "w", True)

        if finish_status == "success":
            shutil.rmtree(tmpdir)

        # send the result to email
        if myfunc.IsValidEmailAddress(email):#{{{

            if os.path.exists(errfile):
                err_msg = myfunc.ReadFile(errfile)

            from_email = "info@topcons.net"
            to_email = email
            subject = "Your result for TOPCONS2 JOBID=%s"%(jobid)
            if finish_status == "success":
                bodytext = """
    Your result is ready at %s/pred/result/%s

    Thanks for using TOPCONS2

            """%(base_www_url, jobid)
            elif finish_status == "failed":
                bodytext="""
    We are sorry that your job with jobid %s is failed.

    Please contact %s if you have any questions.

    Attached below is the error message:
    %s
                """%(jobid, contact_email, err_msg)
            else:
                bodytext="""
    Your result is ready at %s/pred/result/%s

    We are sorry that TOPCONS failed to predict some sequences of your job.

    Please re-submit the queries that have been failed.

    If you have any further questions, please contact %s.

    Attached below is the error message:
    %s
                """%(base_www_url, jobid, contact_email, err_msg)

            myfunc.WriteFile("Sendmail %s -> %s, %s"% (from_email, to_email, subject), logfile, "a", True)
            rtValue = myfunc.Sendmail(from_email, to_email, subject, bodytext)
            if rtValue != 0:
                myfunc.WriteFile("Sendmail to {} failed with status {}".format(to_email,
                    rtValue), errfile, "a", True)

#}}}
#}}}
def RunStatistics(path_result, path_log):#{{{
# 1. calculate average running time, only for those sequences with time.txt
# show also runtime of type and runtime -vs- seqlength
    myfunc.WriteFile("RunStatistics...\n", gen_logfile, "a", True)
    finishedjoblogfile = "%s/finished_job.log"%(path_log)
    runtimelogfile = "%s/jobruntime.log"%(path_log)
    runtimelogfile_finishedjobid = "%s/jobruntime_finishedjobid.log"%(path_log)

    finishedjobidlist = myfunc.ReadIDList2(finishedjoblogfile, col=0, delim="\t")
    runtime_finishedjobidlist = myfunc.ReadIDList(runtimelogfile_finishedjobid)
    toana_jobidlist = list(set(finishedjobidlist)-set(runtime_finishedjobidlist))

    for jobid in toana_jobidlist:
        runtimeloginfolist = []
        rstdir = "%s/%s"%(path_result, jobid)
        outpath_result = "%s/%s"%(rstdir, jobid)
        finished_seq_file = "%s/finished_seqs.txt"%(outpath_result)
        lines = myfunc.ReadFile(finished_seq_file).split("\n")
        for line in lines:
            strs = line.split("\t")
            if len(strs)>=7:
                source = strs[4]
                if source == "newrun":
                    subfolder = strs[0]
                    timefile = "%s/%s/%s"%(outpath_result, subfolder, "time.txt")
                    if os.path.exists(timefile) and os.path.getsize(timefile)>0:
                        txt = myfunc.ReadFile(timefile).strip()
                        try:
                            ss2 = txt.split(";")
                            runtime_str = ss2[1]
                            database_mode = ss2[2]
                            runtimeloginfolist.append("\t".join([jobid, subfolder,
                                source, runtime_str, database_mode]))
                        except:
                            sys.stderr.write("bad timefile %s\n"%(timefile))

        if len(runtimeloginfolist)>0:
            myfunc.WriteFile("\n".join(runtimeloginfolist)+"\n",runtimelogfile, "a", True)
        myfunc.WriteFile(jobid+"\n", runtimelogfile_finishedjobid, "a", True)
#}}}

def main(g_params):#{{{
    submitjoblogfile = "%s/submitted_seq.log"%(path_log)
    runjoblogfile = "%s/runjob_log.log"%(path_log)
    finishedjoblogfile = "%s/finished_job.log"%(path_log)
    loop = 0
    while 1:
        date_str = time.strftime("%Y-%m-%d %H:%M:%S")
        avail_computenode_list = myfunc.ReadIDList(computenodefile)
        num_avail_node = len(avail_computenode_list)
        if loop == 0:
            myfunc.WriteFile("[Date: %s] start %s. loop %d\n"%(date_str, progname, loop), gen_logfile, "a", True)
        else:
            myfunc.WriteFile("[Date: %s] loop %d\n"%(date_str, loop), gen_logfile, "a", True)

        CreateRunJoblog(path_result, submitjoblogfile, runjoblogfile,
                finishedjoblogfile, loop)

        if loop % 10 == 0:
            RunStatistics(path_result, path_log)

        if os.path.exists(gen_logfile):
            myfunc.ArchiveFile(gen_logfile, threshold_logfilesize)
        if os.path.exists(gen_errfile):
            myfunc.ArchiveFile(gen_errfile, threshold_logfilesize)
        # For finished jobs, clean data not used for caching

        cntSubmitJobDict = {} # format of cntSubmitJobDict {'node_ip': INT, 'node_ip': INT}
        for node in avail_computenode_list:
            num_queue_job = GetNumSuqJob(node)
            if num_queue_job >= 0:
                cntSubmitJobDict[node] = [num_queue_job,
                        MAX_SUBMIT_JOB_PER_NODE] #[num_queue_job, max_allowed_job]
            else:
                cntSubmitJobDict[node] = [MAX_SUBMIT_JOB_PER_NODE,
                        MAX_SUBMIT_JOB_PER_NODE] #[num_queue_job, max_allowed_job]

# entries in runjoblogfile includes jobs in queue or running
        hdl = myfunc.ReadLineByBlock(runjoblogfile)
        if not hdl.failure:
            lines = hdl.readlines()
            while lines != None:
                for line in lines:
                    strs = line.split("\t")
                    if len(strs) >= 11:
                        jobid = strs[0]
                        email = strs[4]
                        try:
                            numseq = int(strs[5])
                        except:
                            numseq = 1
                            pass
                        try:
                            numseq_this_user = int(strs[10])
                        except:
                            numseq_this_user = 1
                            pass
                        rstdir = "%s/%s"%(path_result, jobid)
                        finishtagfile = "%s/%s"%(rstdir, "runjob.finish")
                        status = strs[1]

                        if IsHaveAvailNode(cntSubmitJobDict):
                            SubmitJob(jobid, cntSubmitJobDict, numseq_this_user)
                        GetResult(jobid) # the start tagfile is written when got the first result
                        CheckIfJobFinished(jobid, numseq, email)

                lines = hdl.readlines()
            hdl.close()

        myfunc.WriteFile("sleep for %d seconds\n"%(SLEEP_INTERVAL), gen_logfile, "a", True)
        time.sleep(SLEEP_INTERVAL)
        loop += 1


    return 0
#}}}


def InitGlobalParameter():#{{{
    g_params = {}
    g_params['isQuiet'] = True
    return g_params
#}}}
if __name__ == '__main__' :
    g_params = InitGlobalParameter()

    date_str = time.strftime("%Y-%m-%d %H:%M:%S")
    print >> sys.stderr, "\n\n[Date: %s]\n"%(date_str)
    status = main(g_params)

    sys.exit(status)
