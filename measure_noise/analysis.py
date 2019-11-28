import numpy as np

import mo_math
from jx_python import jx
from jx_sqlite.container import Container
from measure_noise import deviance
from measure_noise.extract_perf import get_all_signatures, get_signature, get_dataum
from measure_noise.step_detector import find_segments, MAX_POINTS
from measure_noise.utils import assign_colors
from mo_collections import left
from mo_dots import Null, wrap, Data, coalesce
from mo_future import text, first
from mo_logs import Log, startup, constants
from mo_math.stats import median
from mo_threads import Queue, Thread
from mo_times import MONTH, Date, Timer
from mo_times.dates import parse

IGNORE_TOP = 3  # WHEN CALCULATING NOISE OR DEVIANCE, IGNORE SOME EXTREME VALUES
LOCAL_RETENTION = "3day"  # HOW LONG BEFORE WE REFRESH LOCAL DATABASE ENTRIES


config = Null
local_container = Null
summary_table = Null
candidates = Null


def process(sig_id, show=False, show_limit=MAX_POINTS):
    if not mo_math.is_integer(sig_id):
        Log.error("expecting integer id")
    sig = first(get_signature(config.database, sig_id))
    data = get_dataum(config.database, sig_id)

    min_date = (Date.today() - 3 * MONTH).unix
    pushes = wrap(
        [
            {"value": median(rows.value), "runs": rows, **t}
            for t, rows in jx.groupby(data, "push.time")
            if t["push\\.time"] > min_date
        ]
    )

    values = pushes.value

    title = "-".join(
        map(
            text,
            [
                sig.id,
                sig.framework,
                sig.suite,
                sig.test,
                sig.platform,
                sig.repository.name,
            ],
        )
    )
    Log.note("With {{title}}", title=title)

    with Timer("find segments"):
        new_segments, diffs = find_segments(
            values, sig.alert_change_type, sig.alert_threshold
        )

    old_alerts = [p for p in pushes if any(r.alert.id for r in p.runs)]
    old_segments = tuple(
        sorted(
            set(
                [0]
                + [
                    i
                    for i, p in enumerate(old_alerts)
                    if any(r.alert.id for r in p.runs)
                ]
                + [len(pushes)]
            )
        )
    )

    if len(new_segments) == 1:
        dev_status = None
        dev_score = None
        relative_noise = None
    else:
        # MEASURE DEVIANCE (HOW TO KNOW THE START POINT?)
        s, e = new_segments[-2], new_segments[-1]
        last_segment = np.array(values[s:e])
        trimmed_segment = last_segment[np.argsort(last_segment)[IGNORE_TOP:-IGNORE_TOP]]
        dev_status, dev_score = deviance(trimmed_segment)
        relative_noise = np.std(trimmed_segment) / np.mean(trimmed_segment)
        Log.note(
            "\n\tdeviance = {{deviance}}\n\tnoise={{std}}",
            title=title,
            deviance=(dev_status, dev_score),
            std=relative_noise,
        )

    # CHECK FOR OLD ALERTS
    max_diff = None
    is_diff = new_segments != old_segments
    if is_diff:
        # FOR MISSING POINTS, CALC BIGGEST DIFF
        max_diff = mo_math.MAX(
            d for s, d in zip(new_segments, diffs) if s not in old_segments
        )

        Log.alert("Disagree")
        Log.note("old={{old}}, new={{new}}", old=old_segments, new=new_segments)
        if show and len(pushes):
            assign_colors(values, old_segments, title="OLD " + title)
            assign_colors(values, new_segments, title="NEW " + title)
    else:
        Log.note("Agree")
        if show and len(pushes):
            assign_colors(values, old_segments, title="OLD " + title)
            assign_colors(values, new_segments, title="NEW " + title)

    summary_table.upsert(
        where={"eq": {"id": sig.id}},
        doc=Data(
            id=sig.id,
            title=title,
            num_pushes=len(pushes),
            is_diff=is_diff,
            max_diff=max_diff,
            num_new_segments=len(new_segments),
            num_old_segments=len(old_segments),
            relative_noise=relative_noise,
            dev_status=dev_status,
            dev_score=dev_score,
            last_updated=Date.now(),
        ),
    )


def is_diff(A, B):
    return A != B
    # if len(A) != len(B):
    #     return True
    #
    # for a, b in zip(A, B):
    #     if b - 5 <= a <= b + 5:
    #         continue
    #     else:
    #         return True
    # return False


def update_local_database():
    # GET EVERYTHING WE HAVE SO FAR
    exists = summary_table.query(
        {
            "select": ["id", "last_updated"],
            "where": {"and": [{"in": {"id": candidates.id}}, {"exists": "num_pushes"}]},
            "sort": "last_updated",
            "limit": 100000,
            "format": "list",
        }
    ).data
    # CHOOSE MISSING, THEN OLDEST, UP TO "RECENT"
    missing = list(set(candidates.id) - set(exists.id))

    too_old = (Date.today() - parse(LOCAL_RETENTION)).unix
    needs_update = missing + [e for e in exists if e.last_updated < too_old]
    Log.alert("{{num}} series are candidates for local update", num=len(needs_update))

    limited_update = Queue("sigs")
    limited_update.extend(left(needs_update, coalesce(config.analysis.limit, 100)))
    with Timer(
        "Updating local database with  {{num}} series", {"num": len(limited_update)}
    ):

        def loop(please_stop):
            while not please_stop:
                sig_id = limited_update.pop_one()
                if not sig_id:
                    return
                process(sig_id)

        threads = [Thread.run(text(i), loop) for i in range(3)]
        for t in threads:
            t.join()

    Log.note("Local database is up to date")


def show_sorted(sort):
    tops = summary_table.query(
        {
            "select": "id",
            "where": {
                "and": [{"in": {"id": candidates.id}}, {"gte": {"num_pushes": 1}}]
            },
            "sort": sort,
            "limit": config.args.noise,
            "format": "list",
        }
    ).data

    for id in tops:
        process(id, show=True)


def main():
    global local_container, summary_table, candidates
    local_container = Container(db=config.analysis.local_db)
    summary_table = local_container.get_or_create_facts("perf_summary")

    if config.args.id:
        # EXIT EARLY AFTER WE GOT THE SPECIFIC IDS
        for id in config.args.id:
            process(id, show=True)
        return

    candidates = get_all_signatures(config.database, config.analysis.signatures_sql)
    if not config.args.now:
        update_local_database(summary_table, candidates)

    # DEVIANT
    if config.args.deviant:
        show_sorted({"value": {"abs": "max_diff"}, "sort": "desc"})

    # NOISE
    if config.args.noise:
        show_sorted({"value": {"abs": "relative_noise"}, "sort": "desc"})

    # MISSING
    if config.args.missing:
        show_sorted({"value": {"abs": "max_diff"}, "sort": "desc"})


if __name__ == "__main__":
    config = startup.read_settings(
        [
            {
                "name": ["--id", "--key", "--ids", "--keys"],
                "dest": "id",
                "nargs": "*",
                "type": int,
                "help": "show specific signatures",
            },
            {
                "name": "--now",
                "dest": "now",
                "help": "do not update signatures, go direct to showing problems with what is known locally",
                "action": "store_true",
            },
            {
                "name": ["--dev", "--deviant", "--deviance"],
                "dest": "deviant",
                "nargs": "?",
                "const": 10,
                "type": int,
                "help": "show number of top deviant series",
                "action": "store",
            },
            {
                "name": ["--noise", "--noisy"],
                "dest": "noise",
                "nargs": "?",
                "const": 10,
                "type": int,
                "help": "show number of top noisiest series",
                "action": "store",
            },
            {
                "name": ["--missing", "--missing-alerts"],
                "dest": "missing",
                "nargs": "?",
                "const": 10,
                "type": int,
                "help": "show number of missing alerts",
                "action": "store",
            },
        ]
    )
    constants.set(config.constants)
    try:
        Log.start(config.debug)
        main()
    except Exception as e:
        Log.warning("Problem with perf scan", e)
    finally:
        Log.stop()