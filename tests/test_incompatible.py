"""禁配装箱模式核心规则测试(stdlib unittest,无需 Web 依赖)。

运行:python tests/test_incompatible.py
覆盖:非法批号/自身引用拒绝、单向声明双方禁配、总件数 1~10 限制、
同批逐件拆箱、容量/温区/锁温/度时约束、箱数最少与箱号序列字典序最小、
无解不返回部分方案、未启用时沿用原规划,以及容灾/延误/交接对模式快照的沿用。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.planner import plan  # noqa: E402
from app.resilience import assess_zone_failures  # noqa: E402
from app.delay import simulate_delay  # noqa: E402

WIDE_ZONE = [{"name": "全区", "t_min": -30, "t_max": 30}]


def base_payload(**over):
    p = {
        "transport_hours": 8,
        "box_capacity": 4,
        "max_boxes": 10,
        "zones": [{"name": "全区", "t_min": -30, "t_max": 30}],
        "incompatible_mode": True,
        "cargoes": [],
    }
    p.update(over)
    return p


def cargo(name, units, **over):
    c = {"name": name, "units": units, "unit_volume": 1, "t_min": 0, "t_max": 10}
    c.update(over)
    return c


def box_map(result):
    """{箱号: [(批号, 件数), ...]}"""
    return {
        b["box_no"]: [(i["batch_no"], i["units"]) for i in b["items"]]
        for b in result["boxes"]
    }


def piece_sequence(result):
    """按输入批次逐件顺序组成的箱号序列(用于字典序比较)。"""
    seq = []
    for b_no, items in sorted(box_map(result).items()):
        pass
    counts = {}
    for b in result["boxes"]:
        for it in b["items"]:
            counts.setdefault(it["batch_no"], []).extend(
                [b["box_no"]] * it["units"]
            )
    out = []
    for no in sorted(counts):
        out.extend(counts[no])
    return out


class ValidationTest(unittest.TestCase):
    def test_self_reference_rejected(self):
        with self.assertRaises(ValueError):
            plan(base_payload(cargoes=[cargo("A", 1, incompatible_with="1")]))

    def test_out_of_range_rejected(self):
        # 序号 3 超出 2 批
        with self.assertRaises(ValueError):
            plan(
                base_payload(
                    cargoes=[
                        cargo("A", 1, incompatible_with="3"),
                        cargo("B", 1),
                    ]
                )
            )
        # 序号 0 非法
        with self.assertRaises(ValueError):
            plan(base_payload(cargoes=[cargo("A", 1, incompatible_with="0")]))

    def test_non_numeric_rejected(self):
        with self.assertRaises(ValueError):
            plan(base_payload(cargoes=[cargo("A", 1, incompatible_with="x")]))

    def test_total_units_limit(self):
        with self.assertRaises(ValueError):
            plan(base_payload(cargoes=[cargo("A", 11)]))

    def test_one_way_declaration_is_mutual(self):
        # 批1 声明禁配批2(批2 未反向填写),双方仍不得同箱;
        # 容量 4、各 2 件本可同箱,禁配后必须 2 箱
        r = plan(
            base_payload(
                cargoes=[
                    cargo("A", 2, incompatible_with="2"),
                    cargo("B", 2),
                ]
            )
        )
        self.assertTrue(r["feasible"])
        self.assertEqual(r["summary"]["boxes_used"], 2)
        for b in r["boxes"]:
            nos = {i["batch_no"] for i in b["items"]}
            self.assertFalse({1, 2} <= nos)
        self.assertEqual(
            r["forbidden_pairs"],
            [{"batch_no": 1, "other_batch_no": 2}],
        )


class SolverTest(unittest.TestCase):
    def test_min_boxes_and_lex_sequence(self):
        # 3+3+4=10 件,容量 4;禁配 1-2、2-3。最少 3 箱,
        # 字典序最小件序列应为 1,1,1,2,2,2,1,2,3,3 风格(箱号按首次出现)
        r = plan(
            base_payload(
                cargoes=[
                    cargo("A", 3, incompatible_with="2"),
                    cargo("B", 3, incompatible_with="3"),
                    cargo("C", 4),
                ]
            )
        )
        self.assertTrue(r["feasible"])
        self.assertEqual(r["summary"]["boxes_used"], 3)
        seq = piece_sequence(r)
        # 首箱必含批1的首件(箱号1);批2首件必为新箱(箱号2);
        # 批3 首件应尽早进入已有可行箱,不得无谓开新箱
        self.assertEqual(seq[0], 1)
        self.assertEqual(seq[3], 2)
        # 总使用箱数就是最少箱数
        self.assertLessEqual(r["summary"]["boxes_used"], 3)

    def test_same_batch_split_across_boxes(self):
        # 仅 1 批 6 件、容量 4 → 必须拆成 2 箱(同批可逐件拆箱)
        r = plan(base_payload(cargoes=[cargo("A", 6)]))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["summary"]["boxes_used"], 2)
        units = sorted(
            sum(u for _, u in items) for items in box_map(r).values()
        )
        self.assertEqual(units, [2, 4])

    def test_capacity_respected(self):
        # 两件体积 3,容量 4:不能同箱
        r = plan(
            base_payload(
                cargoes=[
                    cargo("A", 1, unit_volume=3),
                    cargo("B", 1, unit_volume=3),
                ]
            )
        )
        self.assertEqual(r["summary"]["boxes_used"], 2)

    def test_temperature_separation(self):
        # 无禁配声明,但允许温区不相交 → 必须分箱
        r = plan(
            base_payload(
                cargoes=[
                    cargo("冷", 2, t_min=-5, t_max=0),
                    cargo("热", 2, t_min=8, t_max=12),
                ]
            )
        )
        self.assertTrue(r["feasible"])
        self.assertEqual(r["summary"]["boxes_used"], 2)

    def test_segment_temp_infeasible(self):
        # 批2 在第 2 段无温区可覆盖:任何方案都不可行,不返回部分方案
        r = plan(
            base_payload(
                transport_hours=None,
                zones=[],
                segments=[
                    {"name": "去程", "hours": 4, "zones": WIDE_ZONE},
                    {
                        "name": "回程",
                        "hours": 4,
                        "zones": [{"name": "冷藏", "t_min": 0, "t_max": 8}],
                    },
                ],
                cargoes=[
                    cargo("正常", 1, t_min=2, t_max=6),
                    cargo("冷冻", 1, t_min=-25, t_max=-15),
                ],
            )
        )
        self.assertFalse(r["feasible"])
        self.assertEqual(r["boxes"], [])
        self.assertIn("冷冻", r["infeasible_reason"])
        self.assertEqual(r["infeasible_batch_no"], 2)

    def test_lock_group_infeasible(self):
        # 相邻两段同锁温组,但可行温度段交集为空 → 无共享设定点 → 无解
        r = plan(
            base_payload(
                transport_hours=None,
                zones=[],
                segments=[
                    {
                        "name": "段1",
                        "hours": 4,
                        "lock_group": "锁",
                        "zones": [{"name": "冷", "t_min": 0, "t_max": 4}],
                    },
                    {
                        "name": "段2",
                        "hours": 4,
                        "lock_group": "锁",
                        "zones": [{"name": "暖", "t_min": 10, "t_max": 14}],
                    },
                ],
                cargoes=[cargo("宽温货", 1, t_min=2, t_max=12)],
            )
        )
        self.assertFalse(r["feasible"])
        self.assertIn("锁温组", r["infeasible_reason"])

    def test_budget_separation(self):
        # 两批温区仅在 2℃ 相交,24h 下鲜奶在 2℃ 累计 24 ℃·h 超过预算 10
        # → 无法与三文鱼同箱,必须分箱
        r = plan(
            base_payload(
                transport_hours=24,
                box_capacity=10,
                cargoes=[
                    cargo(
                        "三文鱼", 2, t_min=-2, t_max=2,
                        preferred_t_min=-1, preferred_t_max=1,
                        max_degree_hours=30,
                    ),
                    cargo(
                        "鲜奶", 2, t_min=2, t_max=6,
                        preferred_t_min=3, preferred_t_max=5,
                        max_degree_hours=10,
                    ),
                ],
            )
        )
        self.assertTrue(r["feasible"])
        self.assertEqual(r["summary"]["boxes_used"], 2)

    def test_max_boxes_exceeded(self):
        # 4 件互斥但 max_boxes=2 → 无解,原因是箱数不足
        r = plan(
            base_payload(
                max_boxes=2,
                cargoes=[
                    cargo("A", 1, incompatible_with="2,3,4"),
                    cargo("B", 1, incompatible_with="3,4"),
                    cargo("C", 1, incompatible_with="4"),
                    cargo("D", 1),
                ],
            )
        )
        self.assertFalse(r["feasible"])
        self.assertEqual(r["boxes"], [])
        self.assertIn("最大箱数", r["infeasible_reason"])

    def test_single_unit_too_large(self):
        r = plan(
            base_payload(
                box_capacity=2,
                cargoes=[cargo("大件", 1, unit_volume=3)],
            )
        )
        self.assertFalse(r["feasible"])
        self.assertIn("超过单箱容量", r["infeasible_reason"])

    def test_no_partial_plan_on_infeasible(self):
        r = plan(
            base_payload(
                max_boxes=1,
                cargoes=[
                    cargo("A", 1, incompatible_with="2"),
                    cargo("B", 1),
                ],
            )
        )
        self.assertFalse(r["feasible"])
        self.assertEqual(r["summary"]["loaded_units"], 0)
        self.assertEqual(r["boxes"], [])


class LegacyModeTest(unittest.TestCase):
    def test_disabled_keeps_ffd(self):
        # 未启用模式:沿用原 FFD,允许部分装载,无 feasible=false
        r = plan(
            {
                "transport_hours": 24,
                "box_capacity": 12,
                "max_boxes": 20,
                "zones": [{"name": "冷藏区", "t_min": 0, "t_max": 8}],
                "cargoes": [
                    {"name": "鲜奶", "units": 40, "unit_volume": 0.25,
                     "t_min": 2, "t_max": 6},
                    {"name": "三文鱼", "units": 10, "unit_volume": 0.4,
                     "t_min": -2, "t_max": 2},
                ],
            }
        )
        self.assertFalse(r["incompatible_mode"])
        self.assertTrue(r["feasible"])
        self.assertEqual(r["summary"]["boxes_used"], 2)
        self.assertEqual(r["forbidden_pairs"], [])

    def test_incompatible_field_ignored_when_disabled(self):
        # 未启用时即便填了批号也不生效(保留原规划)
        r = plan(
            {
                "transport_hours": 1,
                "box_capacity": 10,
                "max_boxes": 10,
                "zones": [{"name": "全区", "t_min": 0, "t_max": 10}],
                "cargoes": [
                    {"name": "A", "units": 2, "unit_volume": 1,
                     "t_min": 0, "t_max": 10, "incompatible_with": "2"},
                    {"name": "B", "units": 2, "unit_volume": 1,
                     "t_min": 0, "t_max": 10},
                ],
            }
        )
        self.assertEqual(r["summary"]["boxes_used"], 1)


class SnapshotPropagationTest(unittest.TestCase):
    def _payload(self):
        return base_payload(
            box_capacity=10,
            transport_hours=None,
            zones=None,
            segments=[
                {
                    "name": "全程段",
                    "hours": 8,
                    "zones": [
                        {"name": "全区", "t_min": -30, "t_max": 30},
                        {"name": "冷藏", "t_min": 0, "t_max": 8},
                    ],
                }
            ],
            cargoes=[
                cargo("A", 2, incompatible_with="2"),
                cargo("B", 2),
            ],
        )

    def test_assessment_carries_mode(self):
        p = self._payload()
        a = assess_zone_failures(p)
        self.assertTrue(a["incompatible_mode"])
        self.assertEqual(
            a["forbidden_pairs"],
            [{"batch_no": 1, "other_batch_no": 2}],
        )
        self.assertTrue(a["baseline"]["feasible"])
        # 全区温区失效后仍有冷藏覆盖两批,禁配仍满足 → 可运输
        scen = next(
            s for s in a["scenarios"] if s["zone_name"] == "全区"
        )
        self.assertTrue(scen["transportable"])

    def test_assessment_infeasible_baseline_rejected(self):
        p = base_payload(
            max_boxes=1,
            cargoes=[
                cargo("A", 1, incompatible_with="2"),
                cargo("B", 1),
            ],
        )
        with self.assertRaises(ValueError):
            assess_zone_failures(p)

    def test_delay_carries_mode(self):
        p = self._payload()
        d = simulate_delay(
            {"plan_input": p, "segment_no": 1, "extra_hours": 0}
        )
        self.assertTrue(d["incompatible_mode"])
        self.assertEqual(
            d["forbidden_pairs"],
            [{"batch_no": 1, "other_batch_no": 2}],
        )
        self.assertTrue(d["can_continue"])
        # 箱号/批次/件数沿用基准:批1与批2仍分处不同箱
        for box in d["boxes"]:
            nos = {it["batch_no"] for it in box["items"]}
            self.assertFalse({1, 2} <= nos)


if __name__ == "__main__":
    unittest.main(verbosity=2)
