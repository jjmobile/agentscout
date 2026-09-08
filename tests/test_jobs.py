from agentscout import jobs

DID_X = "did:key:z6MkmJoGi2y5gNzD3pnfkW9YdG3AJEtXaxUmx1ziMErAoRES"
TAIL = " | reward tier 3/5 | done looks like: one line. | deliver as one signed message in the deal room, then reveal. | PROTOCOL: after accepting, post a heartbeat frame in the derived deal room. | CREDIT: passes are ranked under your DID."

INF_ROWS = ("seq | payer | amount | asset | proto | time "
            "770167 | AiCsjsQ8 | 200 | FLOP | blockrewards | 20:14:38 "
            "922789 | JNXf7eik | 200 | FLOP | a2a | 03:26:23 "
            "377564 | iZDZE2Xz | 400 | FLOP | a2a | 22:33:09 "
            "629214 | AiCsjsQ8 | 400 | FLOP | blockrewards | 13:42:03 "
            "536354 | wDPz2djS | 1000000 | PAPER | blockrewards | 07:04:13")


def inf(question):
    return f"inference | From the note the table at the end of this note (rows: seq | payer | amount | asset | proto | time): {question}{TAIL} | MATERIAL: {INF_ROWS}"


def test_parse_spec_reads_family_question_done_and_table():
    spec = jobs.parse_spec(inf("output the seq values that are even numbers, in ascending order, comma-separated (or 'none')."))
    assert spec.family == "inference" and spec.done == "one line."
    assert spec.columns == ["seq", "payer", "amount", "asset", "proto", "time"]
    assert len(spec.rows) == 5 and spec.rows[0] == ["770167", "AiCsjsQ8", "200", "FLOP", "blockrewards", "20:14:38"]
    assert "MATERIAL" not in spec.question and spec.question.startswith("From the note")


def test_inference_templates():
    assert jobs.solve(jobs.parse_spec(inf("output the seq values that are even numbers, in ascending order, comma-separated (or 'none')."))) == "377564, 536354, 629214"
    assert jobs.solve(jobs.parse_spec(inf("output the seq values of the 3 rows with the largest amount, highest first (ties broken by lower seq first), comma-separated."))) == "536354, 377564, 629214"
    assert jobs.solve(jobs.parse_spec(inf('sum the amount per payer and output the payer with the largest total and that total, as "<payer> <total>" (ties: ASCII-smaller payer).'))) == "wDPz2djS 1000000"
    assert jobs.solve(jobs.parse_spec(inf("sort all rows by payer (ASCII order), then by seq ascending, and output the seq values in that order, comma-separated."))) == "629214, 770167, 922789, 377564, 536354"
    assert jobs.solve(jobs.parse_spec(inf('output the seq of the row with the earliest time and the seq of the row with the latest time, as "<earliest_seq> <latest_seq>" (ties: lower seq).'))) == "922789 377564"


def test_inference_even_none_and_ties():
    odd = INF_ROWS.replace("770167", "770161").replace("922789", "922789").replace("377564", "377561").replace("629214", "629211").replace("536354", "536351")
    spec = jobs.parse_spec(inf("output the seq values that are even numbers, in ascending order, comma-separated (or 'none').").replace(INF_ROWS, odd))
    assert jobs.solve(spec) == "none"


def test_verification_templates():
    table = ("seq | time | type | from | ref "
             f"1 | 04:33 | offer | {DID_X} | 0xaa "
             f"2 | 04:34 | lock | {DID_X} | 0xaa "
             f"3 | 04:35 | lock | did:key:z6MkgX1TpBarUDBeYv5Nzt6QVnZ13vWLA2HWt5GGZ5nd8U8J | 0xbb "
             f"4 | 04:36 | lock | {DID_X} | 0xcc "
             f"5 | 04:37 | receipt | {DID_X} | 0xcc")
    head = "verification | From the note the table at the end of this note (an excerpt of the tclk board, one frame per line: seq | time | type | from | ref): "
    assert jobs.solve(jobs.parse_spec(head + f"how many rows are lock frames posted by {DID_X}? Give the count. This recount is used to verify the public board.{TAIL} | MATERIAL: {table}")) == "2"
    assert jobs.solve(jobs.parse_spec(head + f"how many rows are offer frames posted by {DID_X}, and how many are lock frames by the same sender? Give both.{TAIL} | MATERIAL: {table}")) == "offers 1, locks 2"
    assert jobs.solve(jobs.parse_spec(f'verification | From the note /kv/x/y (one line: 17+25=42): what is the value after "="?{TAIL}')) == "42"


def test_census_templates():
    table = ("seq | id | payer | amount | asset | rails | proto | role "
             "1 | 0x1 | bob | 200 | FLOP | paper | a2a | payer "
             "2 | 0x2 | amy | 300 | PAPER | paper | - | payer "
             "3 | 0x3 | bob | 100 | FLOP | paper,x402 | a2a | payer "
             "4 | 0x4 | cat | 5 | TCK | paper | kibble | payer ")
    head = "census | [difficulty 2/3] From the note /kv/tclk-mat-en/m1 (an excerpt of the tclk-offers board, seq 1–4, one offer per line: seq | id | payer | amount | asset | rails | proto | role): "
    assert jobs.solve(jobs.parse_spec(head + f"Census over the excerpt: how many offers, how many distinct payers, and which payer posted the most (ties: alphabetically first)?{TAIL} | MATERIAL: {table}")) == "offers=4; payers=3; top=bob:2"
    assert jobs.solve(jobs.parse_spec(head + f'Census over the excerpt: count offers per proto value ("-" for none) and report the most common proto with its count, and how many offers list exactly the single rail "paper".{TAIL} | MATERIAL: {table}')) == "proto=a2a:2; paper_only=3"
    assert jobs.solve(jobs.parse_spec(head + f"Census over the excerpt: the number of distinct assets, the asset with the largest total amount (sum of amount over its rows; ties: alphabetically first) and that total as an integer.{TAIL} | MATERIAL: {table}")) == "assets=3; top_asset=FLOP:300"


def test_attest_and_unknown_questions():
    assert jobs.solve(jobs.parse_spec("attest | [difficulty 1/3] Post exactly one signed line in this deal's derived room from the did:key that accepted: `tclk-attest <full contract id>`." + TAIL)) == jobs.ATTEST_ANSWER
    assert jobs.solve(jobs.parse_spec(inf("summarise the table in one witty sentence."))) is None
    assert jobs.solve(jobs.parse_spec("math | [difficulty 1/3] Compute gcd(12, 18) and lcm(12, 18)." + TAIL)) is None
    assert jobs.parse_spec("just some text") is None
    # a template whose material lacks the needed columns is refused, not guessed
    spec = jobs.parse_spec(inf("output the seq values that are even numbers, in ascending order, comma-separated (or 'none').").replace(INF_ROWS, "a | b 1 | 2"))
    assert jobs.solve(spec) is None


def test_context_helpers():
    ctx = "census | From the note /kv/tclk-mat-en/mcensus-6cf077 (an excerpt …): Census over the excerpt: how many offers … | full spec: /kv/tclk-job-en/census-6cf077c"
    assert jobs.context_family(ctx) == "census"
    assert jobs.context_spec_ref(ctx) == ("tclk-job-en", "census-6cf077c")
    assert jobs.context_family("/kv/tclk-job-9e/task-8dfa229e") is None
    assert jobs.context_spec_ref("/kv/tclk-job-9e/task-8dfa229e") == ("tclk-job-9e", "task-8dfa229e")
    assert jobs.context_spec_ref("Prove it or retire the theory forever") is None


def test_material_note_is_fetched_when_the_table_is_not_inline():
    spec = jobs.parse_spec("census | [difficulty 2/3] From the note /kv/tclk-mat-en/mcensus-509dcb (an excerpt of the tclk-offers board, seq 1–2, one offer per line: seq | id | payer | amount | asset | rails | proto | role): Census over the excerpt: how many offers, how many distinct payers, and which payer posted the most (ties: alphabetically first)?" + TAIL)
    assert spec.rows == [] and spec.columns == ["seq", "id", "payer", "amount", "asset", "rails", "proto", "role"]
    assert jobs.material_ref(spec) == ("tclk-mat-en", "mcensus-509dcb")
    jobs.attach_material(spec, "seq | id | payer | amount | asset | rails | proto | role\n1 | 0x1 | amy | 5 | FLOP | paper | a2a | payer\n2 | 0x2 | amy | 5 | FLOP | paper | a2a | payer")
    assert len(spec.rows) == 2 and jobs.solve(spec) == "offers=2; payers=1; top=amy:2"
    assert jobs.material_ref(spec) is None


def test_census_ties_are_broken_case_insensitively_but_inference_ties_by_ascii():
    table = ("seq | id | payer | amount | asset | rails | proto | role "
             "1 | 0x1 | Md8ABjHr | 1 | FLOP | paper | a2a | payer "
             "2 | 0x2 | bKcY9nZd | 1 | FLOP | paper | a2a | payer "
             "3 | 0x3 | dSro7iDF | 1 | FLOP | paper | a2a | payer ")
    head = "census | From the note /kv/m/x (an excerpt of the tclk-offers board, seq 1–3, one offer per line: seq | id | payer | amount | asset | rails | proto | role): "
    assert jobs.solve(jobs.parse_spec(head + f"Census over the excerpt: how many offers, how many distinct payers, and which payer posted the most (ties: alphabetically first)?{TAIL} | MATERIAL: {table}")) == "offers=3; payers=3; top=bKcY9nZd:1"
    rows = INF_ROWS.replace("wDPz2djS | 1000000", "wDPz2djS | 200").replace("iZDZE2Xz | 400", "iZDZE2Xz | 200").replace("629214 | AiCsjsQ8 | 400", "629214 | Zed | 400")
    spec = jobs.parse_spec(inf('sum the amount per payer and output the payer with the largest total and that total, as "<payer> <total>" (ties: ASCII-smaller payer).').replace(INF_ROWS, rows))
    assert jobs.solve(spec) == "Zed 400"
