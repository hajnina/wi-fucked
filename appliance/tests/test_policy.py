"""profiles_for_lan_mode — ADR-020's single point of truth for which service
classes exist under a given LAN configuration.
"""

from __future__ import annotations

from wifucked.policy import BEST_EFFORT, DEFAULT_PROFILES, profiles_for_lan_mode


def test_single_mode_offers_only_best_effort():
    assert profiles_for_lan_mode("single") == (BEST_EFFORT,)


def test_single_mode_never_offers_a_backup_capable_profile():
    """Must stay true per ADR-020: collapsing both classes onto one SSID must
    never let undifferentiated traffic reach BACKUP.
    """
    assert all(not p.may_use_backup for p in profiles_for_lan_mode("single"))


def test_two_bss_and_two_psk_offer_both_profiles():
    assert profiles_for_lan_mode("two_bss") == DEFAULT_PROFILES
    assert profiles_for_lan_mode("two_psk") == DEFAULT_PROFILES
