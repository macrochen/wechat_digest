#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wechat_digest 单元测试
"""

import unittest
from src.main import run


class TestMain(unittest.TestCase):
    """主程序功能测试类。"""

    def test_run_callable(self) -> None:
        """验证 run 函数是否可正常调用。"""
        self.assertTrue(callable(run))


if __name__ == "__main__":
    unittest.main()
