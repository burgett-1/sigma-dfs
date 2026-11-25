from pipeline import news_parse as N

P = [{"name": n, "key": n.lower() + "|T"} for n in
     ["Shai Gilgeous-Alexander", "Karl-Anthony Towns", "Jaren Jackson Jr.", "Nikola Jokic", "Jalen Williams",
      "Jalen Brunson", "Anthony Edwards", "Josh Giddey"]]


def st(text):
    r = N.parse(text, P)
    return ([p["name"] for p in r["players"]], r["status"], r["confident"])


def test_clear_single_player_posts():
    assert st("Karl-Anthony Towns (ankle) has been ruled out for tonight's game") == (["Karl-Anthony Towns"], "OUT", True)
    assert st("Nikola Jokić is questionable vs. LAL") == (["Nikola Jokic"], "QUESTIONABLE", True)
    assert st("Jaren Jackson Jr. (knee) OUT tonight") == (["Jaren Jackson Jr."], "OUT", True)
    assert st("Josh Giddey is probable for Wednesday") == (["Josh Giddey"], "PROBABLE", True)
    assert st("Anthony Edwards will play tonight") == (["Anthony Edwards"], "AVAILABLE", True)
    assert st("Anthony Edwards is a game-time decision") == (["Anthony Edwards"], "QUESTIONABLE", True)
    assert st("Shai Gilgeous-Alexander upgraded to available") == (["Shai Gilgeous-Alexander"], "AVAILABLE", True)


def test_ambiguous_posts_change_nothing():
    # two players
    assert st("Jalen Brunson OUT, Karl-Anthony Towns questionable")[2] is False
    # mixed wording
    assert st("Anthony Edwards questionable but expected to play")[2] is False
    # "not expected to play" is not "expected to play"
    assert st("Josh Giddey not expected to play tonight") == (["Josh Giddey"], "OUT", True)
    # a lower-case "out" in ordinary text is not a status
    assert st("Jalen Williams went out of his way to praise the bench") == (["Jalen Williams"], None, False)
    # partial names never match (no 'Jalen' alone)
    assert st("Jalen is out")[0] == []
