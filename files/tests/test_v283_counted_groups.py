"""Real five-block 515/78/40/3/3 three-digit order and fail-closed proofs."""
from collections import Counter
import json
import unittest

from parser import BetParser
from shared_orders_v282 import complete_shared_evidence, decode_shared_candidates


REAL_ORDER = '''000 002 003 004 007 010 013 015 016 018 019 020 022 023 026 027 029 030 034 040 044 045 047 049 050 051 052 055 056 057 060 061 062 065 066 067 068 069 072 073 080 084 085 086 092 095 096 099 104 107 108 110 112 113 117 118 119 120 121 122 125 126 130 131 132 134 135 141 142 149 154 158 159 160 161 162 164 165 167 169 172 176 178 180 181 185 190 192 195 198 199 200 202 203 204 206 210 213 214 217 219 220 221 223 225 226 227 232 233 241 244 245 248 252 254 255 256 259 260 261 262 264 265 266 267 269 270 272 273 275 277 278 279 280 283 285 286 287 289 291 292 294 296 298 300 301 302 305 306 310 315 317 320 321 327 332 333 336 337 338 341 342 346 347 351 353 355 364 368 370 371 372 373 375 376 377 379 380 381 382 383 384 385 386 387 388 390 391 392 393 397 399 401 402 403 405 406 407 408 409 411 414 415 416 417 418 419 420 421 424 425 426 428 429 430 431 432 434 435 439 444 445 448 450 452 456 457 458 462 464 465 467 468 471 473 475 476 477 482 483 484 486 487 488 490 492 494 497 499 500 503 504 506 509 511 513 514 516 517 518 520 521 522 525 526 529 532 533 536 537 538 539 541 542 543 544 545 546 549 551 554 555 558 559 561 564 568 571 579 580 581 582 584 585 588 589 590 591 593 596 598 601 603 604 606 608 609 611 612 616 617 618 619 620 621 622 625 626 629 630 631 632 634 635 636 638 640 641 645 647 649 654 657 658 661 662 664 666 667 671 672 673 674 676 677 678 679 681 684 685 689 690 691 695 696 697 699 700 701 702 703 706 707 708 709 710 712 714 715 718 720 722 723 725 726 727 728 729 731 732 733 734 736 743 744 748 751 752 754 755 756 761 762 765 766 767 775 776 777 778 781 784 789 790 791 792 794 795 799 802 805 806 808 809 810 813 814 816 818 823 826 827 828 832 833 836 838 839 841 843 844 846 847 848 849 850 851 853 854 857 860 862 863 864 867 868 870 872 876 877 882 885 886 887 889 890 894 895 897 898 899 900 901 902 905 906 907 908 910 911 912 914 915 916 917 920 922 923 928 931 932 933 934 935 936 938 942 943 945 948 949 951 952 953 954 960 964 966 967 969 970 977 978 980 981 982 983 987 988 989 990 993 995 996 997
共515组8倍
112 118 132 134 135 172 176 178 192 195 198 214 232 272 275 278 292 294 296 298 315 332 336 338 372 375 376 392 612 616 618 632 634 635 636 638 672 674 676 678 695 696 712 714 715 718 732 734 736 775 776 778 792 794 795 814 816 818 832 836 838 872 876 894 895 898 912 914 915 916 932 934 935 936 938 978 995 996
共78组4倍
132 134 172 176 178 192 198 232 272 278 292 294 296 298 332 336 338 372 376 392 732 734 736 776 778 792 794 832 836 838 872 876 894 898 932 934 936 938 978 996
共40组8倍
338 383 833
共3组10倍
792 992 996
共3组40倍'''


class CountedGroupTests(unittest.TestCase):
    def test_actual_five_blocks_full_ledger_without_ai(self):
        p = BetParser()
        bets, _, error = p.parse('福彩', REAL_ORDER)
        self.assertEqual(error, '')
        self.assertEqual(len(bets), 515 + 78 + 40 + 3 + 3)
        self.assertEqual(Counter(b.multiplier for b in bets), {8: 555, 4: 78, 10: 3, 40: 3})
        self.assertEqual(sum(b.principal for b in bets), 9804)
        self.assertTrue(all(b.play == '直选' for b in bets))
        self.assertTrue(complete_shared_evidence(REAL_ORDER, bets))
        # Numbers occurring in multiple groups are separate purchases, not deduplicated.
        self.assertEqual(sum(b.multiplier for b in bets if b.selection == '338'), 30)
        self.assertEqual(sum(b.multiplier for b in bets if b.selection == '792'), 60)
        self.assertEqual(sum(b.multiplier for b in bets if b.selection == '996'), 60)
        self.assertFalse(p.last_warnings)

    def test_declared_group_mismatch_stops_entire_order(self):
        bets, _, error = BetParser().parse('福彩', REAL_ORDER.replace('共515组8倍', '共516组8倍'))
        self.assertEqual(bets, [])
        self.assertIn('声明516组', error)

    def test_ordinary_summary_count_unit_not_renamed_to_group_play(self):
        b, _, error = BetParser().parse('福彩', '001 002 003\n共3组0.5倍')
        self.assertEqual(error, '')
        self.assertEqual([(x.play, x.multiplier, x.principal) for x in b],
                         [('直选', .5, 1.0)] * 3)

    def test_unknown_summary_text_cannot_be_ignored(self):
        bets, _, error = BetParser().parse('福彩', '001 002 003\n共3组8倍\n还有两注')
        self.assertEqual(bets, [])
        self.assertTrue(error)

    def test_ai_cannot_replace_five_blocks_with_one_guessed_bet(self):
        wrong = {'candidate_bets': [{'segment_id': ['S1', 'S2'], 'bet_type': '直选',
                                    'ticket_codes': ['000'], 'bet_multiples': 8}],
                 'needs_review': False}
        accepted, _, issues, review = decode_shared_candidates(
            REAL_ORDER, json.dumps(wrong), BetParser(), '福彩')
        self.assertEqual(accepted, [])
        self.assertTrue(review)
        self.assertIn('遗漏', issues[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
