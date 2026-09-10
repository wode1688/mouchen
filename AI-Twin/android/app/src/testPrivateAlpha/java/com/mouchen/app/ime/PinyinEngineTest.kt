package com.mouchen.app.ime

import java.io.ByteArrayInputStream
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PinyinEngineTest {
    @Test
    fun naturalCodeUsesCanonicalMappings() {
        assertEquals(setOf("ni"), NaturalCodeScheme.encodeSyllable("ni"))
        assertEquals(setOf("hk"), NaturalCodeScheme.encodeSyllable("hao"))
        assertEquals(setOf("vs"), NaturalCodeScheme.encodeSyllable("zhong"))
        assertEquals(setOf("go"), NaturalCodeScheme.encodeSyllable("guo"))
        assertEquals(setOf("ud"), NaturalCodeScheme.encodeSyllable("shuang"))
        assertEquals(setOf("uh"), NaturalCodeScheme.encodeSyllable("shang"))
        assertEquals(setOf("ih"), NaturalCodeScheme.encodeSyllable("chang"))
        assertEquals(setOf("pn"), NaturalCodeScheme.encodeSyllable("pin"))
        assertEquals(linkedSetOf("al", "ai"), NaturalCodeScheme.encodeSyllable("ai"))
        assertEquals(linkedSetOf("aj", "an"), NaturalCodeScheme.encodeSyllable("an"))
        assertEquals(linkedSetOf("ak", "ao"), NaturalCodeScheme.encodeSyllable("ao"))
        assertEquals(linkedSetOf("ez", "ei"), NaturalCodeScheme.encodeSyllable("ei"))
        assertEquals(linkedSetOf("ef", "en"), NaturalCodeScheme.encodeSyllable("en"))
        assertEquals(linkedSetOf("ob", "ou"), NaturalCodeScheme.encodeSyllable("ou"))
        assertEquals(setOf("eg"), NaturalCodeScheme.encodeSyllable("eng"))
        assertEquals(setOf("yr"), NaturalCodeScheme.encodeSyllable("yuan"))
        assertEquals(setOf("yy"), NaturalCodeScheme.encodeSyllable("ying"))
        assertEquals(setOf("jt"), NaturalCodeScheme.encodeSyllable("jue"))
        assertEquals(setOf("lt"), NaturalCodeScheme.encodeSyllable("lüe"))
        assertEquals(linkedSetOf("ju", "jv"), NaturalCodeScheme.encodeSyllable("ju"))
        assertEquals(setOf("ch"), NaturalCodeScheme.encodeSyllable("cang"))
        assertEquals(setOf("sh"), NaturalCodeScheme.encodeSyllable("sang"))
        assertEquals(setOf("zh"), NaturalCodeScheme.encodeSyllable("zang"))
        assertEquals(setOf("hng"), NaturalCodeScheme.encodeSyllable("hng"))
    }

    @Test
    fun segmentedNaturalCodeCreatesPhraseCode() {
        assertEquals(setOf("nihk"), NaturalCodeScheme.encodeSegmented("ni'hao"))
        assertEquals(setOf("vsgo"), NaturalCodeScheme.encodeSegmented("zhong'guo"))
        assertEquals(setOf("zirjma"), NaturalCodeScheme.encodeSegmented("zi'ran'ma"))
    }

    @Test
    fun fullPinyinAndNaturalCodeShareCandidates() {
        val engine = engine()

        assertEquals("你好", engine.candidates("nihao", ImeInputMode.FULL_PINYIN).first().text)
        assertEquals("你好", engine.candidates("nihk", ImeInputMode.NATURAL_CODE).first().text)
        assertEquals("中国", engine.candidates("vsgo", ImeInputMode.NATURAL_CODE).first().text)
        assertEquals("居", engine.candidates("jv", ImeInputMode.NATURAL_CODE).first().text)
        assertTrue(engine.candidates("nihao", ImeInputMode.ENGLISH).isEmpty())
    }

    @Test
    fun partialInputOffersPhraseCompletion() {
        val results = engine().candidates("nih", ImeInputMode.FULL_PINYIN)
        assertTrue(results.any { it.text == "你好" })
    }

    @Test
    fun prefixSearchDoesNotDropHighFrequencyLateKeys() {
        val rows = buildString {
            append("# segmented_pinyin<TAB>word<TAB>frequency\n")
            repeat(1_005) { index ->
                val suffix = charArrayOf(
                    'a' + (index / (26 * 26)) % 26,
                    'a' + (index / 26) % 26,
                    'a' + index % 26,
                ).concatToString()
                append("she$suffix\t生$index\t1\n")
            }
            append("shi'jian\t时间\t90000000\n")
        }
        val engine = PinyinEngine.load(ByteArrayInputStream(rows.toByteArray(Charsets.UTF_8)))

        assertTrue(engine.candidates("sh", ImeInputMode.FULL_PINYIN).any { it.text == "时间" })
    }

    @Test
    fun learnedCandidateMovesAheadWithoutChangingDictionary() {
        val engine = engine()
        assertEquals("你", engine.candidates("ni", ImeInputMode.FULL_PINYIN).first().text)

        val personalized = engine.candidates(
            "ni",
            ImeInputMode.FULL_PINYIN,
            learnedStats = mapOf("ni\t尼" to ImeLearningStat(10, 1_000_000L)),
            nowMillis = 1_000_000L,
        )

        assertEquals("尼", personalized.first().text)
    }

    @Test
    fun committedPhraseProvidesNextWordAssociation() {
        val suggestions = engine().associations("我们")
        assertTrue(suggestions.any { it.text == "可以" })
    }

    @Test
    fun inputModeCyclesFullNaturalAndEnglish() {
        assertEquals(ImeInputMode.NATURAL_CODE, ImeInputMode.FULL_PINYIN.next())
        assertEquals(ImeInputMode.ENGLISH, ImeInputMode.NATURAL_CODE.next())
        assertEquals(ImeInputMode.FULL_PINYIN, ImeInputMode.ENGLISH.next())
    }

    @Test
    fun bundledLexiconServesCommonWordsAndProductPhrases() {
        val asset = listOf(
            File("src/privateAlpha/assets/ime/pinyin_lexicon_v1.tsv"),
            File("app/src/privateAlpha/assets/ime/pinyin_lexicon_v1.tsv"),
            File("android/app/src/privateAlpha/assets/ime/pinyin_lexicon_v1.tsv"),
        ).firstOrNull(File::isFile)
        requireNotNull(asset) { "Bundled IME lexicon is missing" }
        val bundled = asset.inputStream().use(PinyinEngine::load)

        assertEquals("你好", bundled.candidates("nihao", ImeInputMode.FULL_PINYIN).first().text)
        assertEquals("你好", bundled.candidates("nihk", ImeInputMode.NATURAL_CODE).first().text)
        // Test the checked-in phonetic dictionary, independently of app branding.
        assertEquals("谋臣", bundled.candidates("mouchen", ImeInputMode.FULL_PINYIN).first().text)
        assertEquals("自然码", bundled.candidates("zirjma", ImeInputMode.NATURAL_CODE).first().text)
        assertEquals("爱", bundled.candidates("ai", ImeInputMode.NATURAL_CODE).first().text)
        assertTrue(bundled.candidates("sh", ImeInputMode.FULL_PINYIN).any { it.text == "时间" })
        assertTrue(bundled.candidates("si", ImeInputMode.FULL_PINYIN).first().text != "以")
        assertTrue(bundled.candidates("liao", ImeInputMode.FULL_PINYIN).first().text != "了")
        assertTrue(bundled.candidates("yue", ImeInputMode.FULL_PINYIN).any { it.text == "乐" })
        assertTrue(bundled.candidates("jue", ImeInputMode.FULL_PINYIN).any { it.text == "角" })
        assertTrue(bundled.candidates("jiao", ImeInputMode.FULL_PINYIN).any { it.text == "觉" })
        assertTrue(bundled.candidates("hang", ImeInputMode.FULL_PINYIN).any { it.text == "行" })
        assertTrue(bundled.candidates("chong", ImeInputMode.FULL_PINYIN).any { it.text == "重" })
        assertTrue(bundled.candidates("hh", ImeInputMode.NATURAL_CODE).any { it.text == "行" })
        assertTrue(bundled.candidates("is", ImeInputMode.NATURAL_CODE).any { it.text == "重" })
    }

    private fun engine(): PinyinEngine {
        val source = """
            # segmented_pinyin<TAB>word<TAB>frequency
            ni\t你\t1000
            ni\t尼\t300
            ni'hao\t你好\t2000
            zhong'guo\t中国\t1900
            ju\t居\t400
            wo'men\t我们\t1800
            wo'men'ke'yi\t我们可以\t1700
        """.trimIndent().replace("\\t", "\t")
        return PinyinEngine.load(ByteArrayInputStream(source.toByteArray(Charsets.UTF_8)))
    }
}
