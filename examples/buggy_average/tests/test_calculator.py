import unittest

from calculator import average


class AverageTests(unittest.TestCase):
    def test_average_of_values(self) -> None:
        self.assertEqual(average([2.0, 4.0, 6.0]), 4.0)

    def test_empty_values_return_zero(self) -> None:
        self.assertEqual(average([]), 0.0)


if __name__ == "__main__":
    unittest.main()
