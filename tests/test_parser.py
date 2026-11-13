import pytest

from docksmith.models import ParseError
from docksmith.parser import parse_file, parse_string, split_args


def test_parse_valid_docksmithfile():
    content = """FROM ubuntu:20.04
WORKDIR /app
COPY . /app
ENV KEY=value
RUN echo hello
CMD ["echo","hi"]"""
    insts = parse_string(content)
    assert [(i.op, len(i.args)) for i in insts] == [
        ("FROM", 1), ("WORKDIR", 1), ("COPY", 2), ("ENV", 1), ("RUN", 2), ("CMD", 2)
    ]
    assert all(i.raw for i in insts)
    assert [i.line for i in insts] == [1, 2, 3, 4, 5, 6]


def test_invalid_cmd_requires_json_array():
    with pytest.raises(ParseError, match="JSON array"):
        parse_string("FROM base\nCMD echo hello")


@pytest.mark.parametrize("cmd", ['CMD []', 'CMD {"a": 1}', 'CMD ["a", 1]', "CMD"])
def test_invalid_cmd_variants(cmd):
    with pytest.raises(ParseError, match="line 2: CMD must be JSON array"):
        parse_string(f"FROM base\n{cmd}")


def test_invalid_env():
    with pytest.raises(ParseError, match="invalid ENV format"):
        parse_string("FROM base\nENV KEY value")


def test_unknown_instruction_reports_line():
    with pytest.raises(ParseError, match="line 3: unknown instruction UNKNOWN"):
        parse_string("FROM base\n\nUNKNOWN arg")


def test_out_of_scope_instruction_is_rejected():
    with pytest.raises(ParseError, match="unknown instruction EXPOSE"):
        parse_string("FROM base\nEXPOSE 80")


def test_preserves_raw_string():
    insts = parse_string("FROM   ubuntu:20.04\nRUN    echo    hello")
    assert insts[0].raw == "FROM   ubuntu:20.04"
    assert insts[1].text == "echo    hello"


def test_skips_comments_and_blank_lines():
    insts = parse_string("FROM base\n# comment\n\n   \nRUN echo hello\n\n")
    assert len(insts) == 2


def test_env_with_value_containing_path():
    insts = parse_string("FROM base\nENV PATH=/usr/bin:$PATH")
    assert insts[1].args == ["PATH=/usr/bin:$PATH"]


def test_env_quoted_value_with_spaces():
    insts = parse_string('FROM base\nENV MESSAGE="Hello World"')
    assert insts[1].args == ["MESSAGE=Hello World"]


def test_cmd_valid_json():
    insts = parse_string('FROM base\nCMD ["python", "app.py"]')
    assert insts[1].args == ["python", "app.py"]


def test_crlf_line_endings():
    insts = parse_string("FROM base\r\nRUN echo hi\r\n")
    assert insts[1].raw == "RUN echo hi"


def test_line_continuation():
    insts = parse_string("FROM base\nRUN echo a && \\\n    echo b\nCMD [\"sh\"]")
    assert len(insts) == 3
    assert insts[1].text == "echo a && echo b"
    assert insts[2].line == 4


def test_argument_count_errors():
    for bad, msg in [
        ("FROM", "FROM requires exactly one"),
        ("FROM a b", "FROM requires exactly one"),
        ("COPY onlyone", "COPY requires at least source and destination"),
        ("RUN", "RUN requires a command"),
        ("WORKDIR a b", "WORKDIR requires exactly one"),
    ]:
        with pytest.raises(ParseError, match=msg):
            parse_string(bad if bad.startswith("FROM") else f"FROM base\n{bad}")


def test_unterminated_quote():
    with pytest.raises(ParseError, match="unterminated quoted string"):
        parse_string('FROM base\nRUN echo "oops')


def test_empty_file():
    with pytest.raises(ParseError, match="no valid instructions"):
        parse_string("# only a comment\n")


def test_missing_file(tmp_path):
    with pytest.raises(ParseError, match="not found"):
        parse_file(tmp_path / "Docksmithfile")


def test_split_args():
    assert split_args("a 'b c' \"d e\" f\\ g") == ["a", "b c", "d e", "f g"]
    assert split_args("x ''") == ["x", ""]
