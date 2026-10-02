from plugins.platforms.discord.render import render_for_discord


class TestHorizontalRules:

    def test_dash_rule_dropped(self):
        text = "Para one.\n\n---\n\nPara two."
        out = render_for_discord(text)
        assert "---" not in out
        assert "Para one." in out
        assert "Para two." in out

    def test_asterisk_and_underscore_rules_dropped(self):
        assert "***" not in render_for_discord("a\n***\nb")
        assert "___" not in render_for_discord("a\n___\nb")

    def test_indented_rule_dropped(self):
        assert "---" not in render_for_discord("a\n  ---\nb")

    def test_bullet_list_dash_not_treated_as_rule(self):
        text = "- item one\n- item two"
        assert render_for_discord(text) == text

    def test_bold_text_not_treated_as_rule(self):
        text = "**bold text**"
        assert render_for_discord(text) == text

    def test_table_separator_row_not_touched(self):
        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        assert render_for_discord(text) == text

    def test_rule_inside_fence_untouched(self):
        text = "```\n---\n```"
        assert render_for_discord(text) == text


class TestDeepHeaders:

    def test_h4_collapses_to_h3(self):
        assert render_for_discord("#### Section") == "### Section"

    def test_h6_collapses_to_h3(self):
        assert render_for_discord("###### Deep") == "### Deep"

    def test_h3_unchanged(self):
        assert render_for_discord("### Fine") == "### Fine"

    def test_header_inside_fence_untouched(self):
        text = "```\n#### not a header\n```"
        assert render_for_discord(text) == text


class TestEmptyAndPassthrough:

    def test_empty_string(self):
        assert render_for_discord("") == ""

    def test_plain_text_unchanged(self):
        text = "Just some plain prose with no markdown oddities."
        assert render_for_discord(text) == text
