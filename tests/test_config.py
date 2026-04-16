from vpn_bot.config import load_plans


def test_load_plans_reads_stars_options(tmp_path) -> None:
    plans_file = tmp_path / "plans.toml"
    plans_file.write_text(
        """
[[plans]]
code = "stars_test"
title = "Тест оплаты Stars"
price_rub = "0.00"
price_stars = 1
duration_days = 0
traffic_limit_gb = 0
description = "Проверка оплаты."
provision_access = false
""",
        encoding="utf-8",
    )

    plan = load_plans(plans_file)["stars_test"]

    assert plan.supports_stars
    assert not plan.supports_transfer
    assert not plan.provision_access
    assert plan.price_stars == 1
