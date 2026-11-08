# -*- coding: utf-8 -*-
"""Run on installed production sources in the installer's TEMP sandbox."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import json
import unittest
from parser import BetParser, split_lottery_message
from shared_orders_v282 import (parse_shared_order, decode_shared_candidates,
                               complete_shared_evidence, NAME)

POOLS = '福\n2348     \n3458   \n3568\n组六各20米组三各10米🈴90米'
DIRECT = '''003 023 030 032 034 037 043 073 203 230 403 430
福12注

028 058 068 082 085 086 208 280 508 580 608 680 802 805 806 820 850 860
福18注

226 256 262 265 266
福5注

025 052 205 250 257 258 275 285 238 283
福10注
直选一倍🈴90米'''
BASE515 = '''000 002 003 004 007 010 013 015 016 018 019 020 022 023 026 027 029 030 034 040 044 045 047 049 050 051 052 055 056 057 060 061 062 065 066 067 068 069 072 073 080 084 085 086 092 095 096 099 104 107 108 110 112 113 117 118 119 120 121 122 125 126 130 131 132 134 135 141 142 149 154 158 159 160 161 162 164 165 167 169 172 176 178 180 181 185 190 192 195 198 199 200 202 203 204 206 210 213 214 217 219 220 221 223 225 226 227 232 233 241 244 245 248 252 254 255 256 259 260 261 262 264 265 266 267 269 270 272 273 275 277 278 279 280 283 285 286 287 289 291 292 294 296 298 300 301 302 305 306 310 315 317 320 321 327 332 333 336 337 338 341 342 346 347 351 353 355 364 368 370 371 372 373 375 376 377 379 380 381 382 383 384 385 386 387 388 390 391 392 393 397 399 401 402 403 405 406 407 408 409 411 414 415 416 417 418 419 420 421 424 425 426 428 429 430 431 432 434 435 439 444 445 448 450 452 456 457 458 462 464 465 467 468 471 473 475 476 477 482 483 484 486 487 488 490 492 494 497 499 500 503 504 506 509 511 513 514 516 517 518 520 521 522 525 526 529 532 533 536 537 538 539 541 542 543 544 545 546 549 551 554 555 558 559 561 564 568 571 579 580 581 582 584 585 588 589 590 591 593 596 598 601 603 604 606 608 609 611 612 616 617 618 619 620 621 622 625 626 629 630 631 632 634 635 636 638 640 641 645 647 649 654 657 658 661 662 664 666 667 671 672 673 674 676 677 678 679 681 684 685 689 690 691 695 696 697 699 700 701 702 703 706 707 708 709 710 712 714 715 718 720 722 723 725 726 727 728 729 731 732 733 734 736 743 744 748 751 752 754 755 756 761 762 765 766 767 775 776 777 778 781 784 789 790 791 792 794 795 799 802 805 806 808 809 810 813 814 816 818 823 826 827 828 832 833 836 838 839 841 843 844 846 847 848 849 850 851 853 854 857 860 862 863 864 867 868 870 872 876 877 882 885 886 887 889 890 894 895 897 898 899 900 901 902 905 906 907 908 910 911 912 914 915 916 917 920 922 923 928 931 932 933 934 935 936 938 942 943 945 948 949 951 952 953 954 960 964 966 967 969 970 977 978 980 981 982 983 987 988 989 990 993 995 996 997'''
SECOND33 = '002 003 013 015 016 085 086 092 095 096 601 603 606 611 612 616 681 685 691 695 696 792 794 802 805 806 813 816 882 885 886 895 898'
BIG = BASE515+'\n515注6倍\n\n'+SECOND33+'\n35注6倍\n\n092 096 792 794 898\n5注60倍'


def due(bets):return sum(Decimal(str(b.principal)) for b in bets)


class SharedOrders(unittest.TestCase):
    def success(self, text, total, count, lottery='福彩'):
        p=BetParser();b,_,reason=p.parse(lottery,text)
        self.assertFalse(reason,reason);self.assertEqual(len(b),count);self.assertEqual(due(b),Decimal(str(total)))
        self.assertTrue(all(x.parse_source=='local' for x in b))
        return p,b

    def test_original_three_pools(self):
        p,b=self.success(POOLS,90,6)
        self.assertEqual(Counter((x.play,x.multiplier) for x in b),Counter({('四码组六',2):3,('四码组三',1):3}))
        self.assertEqual(p.last_claimed,90);self.assertFalse(p.last_warnings)

    def test_original_shared_total_inline(self):
        p,b=self.success(DIRECT,90,45);self.assertEqual(p.last_claimed,90)
        self.assertEqual(len({x.selection for x in b}),45)

    def test_original_count_typo_553_bets(self):
        self.assertEqual(len(BASE515.split()),515)
        self.assertEqual(len(SECOND33.split()),33)
        p,b=self.success(BIG,7176,553)
        self.assertTrue(any('35注' in s and '33注' in s for s in p.last_warnings))
        mult=Counter()
        for x in b:mult[x.selection]+=x.multiplier
        self.assertEqual(Counter(mult.values()),Counter({6:482,12:28,72:5}))
        for code in ('092','096','792','794','898'):self.assertEqual(mult[code],72)

    def test_user_shared_multiplier_without_receipt(self):
        self.success(DIRECT.replace('🈴90米',''),90,45)

    def test_receipt_separate_line(self):
        self.success(DIRECT.replace('🈴90米','\n合计90米'),90,45)

    def test_blank_crlf_fullwidth(self):
        text=POOLS.replace('\n','\r\n\r\n').replace('20','２０').replace('10','１０')
        self.success(text,90,6)

    def test_variable_prices_not_hardcoded(self):
        p=BetParser();rules=deepcopy(p.rules)
        rules['福彩']['四码组六']['principal']=5
        rules['福彩']['四码组三']['principal']=20
        p=BetParser(rules);b,_,reason=p.parse('福彩',POOLS)
        self.assertFalse(reason);self.assertEqual(due(b),90)
        self.assertEqual(Counter((x.play,x.multiplier) for x in b),Counter({('四码组六',4):3,('四码组三',.5):3}))

    def test_all_pool_sizes_shared(self):
        for n in range(4,10):
            for play in ('组三','组六','复试'):
                with self.subTest(n=n,play=play):
                    text='福\n'+'012345678'[:n]+'\n'+'123456789'[:n]+'\n'+play+'各20米合40米'
                    self.success(text,40,2)

    def test_sport_lottery(self):
        self.success(POOLS.replace('福','排',1),90,6,'体彩')

    def test_repeat_across_groups_preserved(self):
        self.success('001 002\n2注2倍\n001\n1注3倍',14,3)

    def test_repeat_within_group_preserved(self):
        p,b=self.success('001 001\n2注1倍',4,2)
        self.assertTrue(any('重复' in w for w in p.last_warnings))

    def test_correct_count_not_redefined_as_multiples(self):
        self.success('001 002\n福100注6倍',24,2)

    def test_shared_applies_only_unpriced(self):
        p,b=self.success('001\n福1注3倍\n002\n福1注\n直选两倍合4米',10,2)
        self.assertEqual([(x.selection,x.multiplier) for x in b],[('001',3),('002',2)])

    def test_default_direct_cash_count(self):
        self.success('001 002\n福2注一块合2米',2,2)

    def test_half_yuan_and_fraction(self):
        self.success('001 002\n福2注\n直选各0.5米合1米',1,2)
        self.success('001 002\n福2注\n直选1/4倍合1米',1,2)

    def test_front_headers_still_work(self):
        self.success('福直选2倍\n001 002\n福直选3倍\n003',14,3)

    def test_no_unit_pool_convention_preserved(self):
        p,b=self.success('13589\n福组六200',200,1)
        self.assertEqual(b[0].multiplier,20)

    def test_real_evidence_spans(self):
        _,b=self.success(POOLS,90,6)
        for x in b:
            span=x.source_span
            self.assertEqual(span['text'],POOLS[span['start']:span['end']])
            self.assertIn('S5',span['segment_id'])
            for e in span['evidence_segments']:self.assertEqual(e['text'],POOLS[e['start']:e['end']])
        self.assertEqual(b[0].source_span['selection_segment_ids'],['S2'])
        self.assertTrue(complete_shared_evidence(POOLS,b))
        self.assertFalse(complete_shared_evidence(POOLS,b[:-1]))

    def test_no_generated_extra_number(self):
        _,b=self.success('001 002\n福3注1倍',4,2)
        self.assertEqual([x.selection for x in b],['001','002'])

    def test_invalid_and_unsupported_do_not_succeed(self):
        cases=['福\n12345\n四码组六2倍','福\n1123\n四码组六2倍',
               '001 002\n福2注','001 002\n2注\n说明文字\n直选1倍',
               '001 002\n2注\n直选1/3倍','福\n2348\n体组六20米',
               '001 002\n2注\n直选0倍','001 002\n2注\n直选负2倍',
               '001 002\n2注1倍\n合4米\n003',
               '001 002\n2注\n直选4/1倍']
        for text in cases:
            with self.subTest(text=text):
                r=parse_shared_order(BetParser(),'福彩',text)
                self.assertIsNotNone(r);self.assertFalse(r[0]);self.assertTrue(r[2])

    def test_outside_grammar_preserved(self):
        for text in ('福1479四码组六100🈴100','123直选1倍','001 002',POOLS+'\n取消',
                     '001\n直选1倍\n福1479四码组六2倍', '001\n直选1倍\n和值10打20米'):
            with self.subTest(text=text):self.assertIsNone(parse_shared_order(BetParser(),'福彩',text))

    def test_routing_then_local_acceptance(self):
        for text,total,count in ((POOLS,90,6),(DIRECT,90,45),(BIG,7176,553)):
            sections=split_lottery_message(text,'福彩')
            self.assertEqual(set(sections),{'福彩'})
            self.success(sections['福彩'],total,count)


class SharedAI(unittest.TestCase):
    def candidate(self):
        items=[]
        for code,sid in [('2348','S2'),('3458','S3'),('3568','S4')]:
            for kind,price in [('四码组六',20),('四码组三',10)]:
                items.append({'segment_id':sid,'bet_type':kind,'digit_set':code,
                    'bet_multiples':None,'declared_amount_yuan':price,'amount_kind':'amount'})
        return {'candidate_bets':items,'needs_review':False,'uncertainties':[]}

    def decode(self,obj):return decode_shared_candidates(POOLS,json.dumps(obj),BetParser(),'福彩')

    def test_short_references_gain_proven_instruction(self):
        b,items,errors,uncertain=self.decode(self.candidate())
        self.assertFalse(errors);self.assertFalse(uncertain);self.assertEqual(len(b),6);self.assertEqual(due(b),90)
        self.assertTrue(complete_shared_evidence(POOLS,b))
        self.assertTrue(all('S5' in x.source_span['segment_id'] for x in b))

    def test_ai_invented_reference_rejected(self):
        d=self.candidate();d['candidate_bets'][0]['segment_id']='S99'
        self.assertFalse(self.decode(d)[0])

    def test_ai_wrong_number_reference_rejected(self):
        d=self.candidate();d['candidate_bets'][0]['segment_id']='S3'
        self.assertFalse(self.decode(d)[0])

    def test_ai_fake_evidence_text_rejected(self):
        d=self.candidate();d['candidate_bets'][0]['evidence_text']='2348组六2000倍'
        self.assertFalse(self.decode(d)[0])

    def test_ai_invented_number_rejected(self):
        d=self.candidate();d['candidate_bets'][0]['digit_set']='2349'
        self.assertFalse(self.decode(d)[0])

    def test_ai_wrong_multiplier_rejected(self):
        d=self.candidate();d['candidate_bets'][0]['bet_multiples']=20
        self.assertFalse(self.decode(d)[0])

    def test_ai_total_not_repeated_per_ticket(self):
        d=self.candidate();d['candidate_bets'][0]['declared_amount_yuan']=90
        self.assertFalse(self.decode(d)[0])

    def test_ai_omission_rejected(self):
        d=self.candidate();d['candidate_bets'].pop()
        self.assertFalse(self.decode(d)[0])

    def test_ai_duplicate_rejected(self):
        d=self.candidate();d['candidate_bets'].append(d['candidate_bets'][0].copy())
        self.assertFalse(self.decode(d)[0])

    def test_ai_boolean_rejected(self):
        d=self.candidate();d['candidate_bets'][1]['bet_multiples']=True
        self.assertFalse(self.decode(d)[0])

    def test_ai_model_uncertainty_not_silently_ignored(self):
        d=self.candidate();d['needs_review']=True
        self.assertFalse(self.decode(d)[0])

if __name__=='__main__':unittest.main(verbosity=2)
