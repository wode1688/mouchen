package com.mouchen.app.localization

import java.util.Locale
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class MouchenLocalizationTest {
    @Test
    fun onlySupportedAccountLocalesAreAccepted() {
        assertEquals(LOCALE_ZH_CN, normalizeMouchenLocale("zh_CN"))
        assertEquals(LOCALE_EN_US, normalizeMouchenLocale("en-US"))
        assertNull(normalizeMouchenLocale("fr-FR"))
    }

    @Test
    fun preLoginLanguageFollowsTheSystem() {
        assertEquals(LOCALE_ZH_CN, systemMouchenLocale(Locale.SIMPLIFIED_CHINESE))
        assertEquals(LOCALE_EN_US, systemMouchenLocale(Locale.US))
        assertEquals(LOCALE_EN_US, systemMouchenLocale(Locale.JAPAN))
    }

    @Test
    fun applicationChromeIsLocalizedButUnknownEvidenceIsUntouched() {
        assertEquals("Advice", localizeUiText("谏言", LOCALE_EN_US))
        assertEquals("Collection", localizeUiText("采集", LOCALE_EN_US))
        assertEquals("Queued", localizeUiText("等待执行", LOCALE_EN_US))
        assertEquals("First step: verify the invoice", localizeUiText("第一步：verify the invoice", LOCALE_EN_US))
        val evidence = "对方原话：明天付款"
        assertEquals(evidence, localizeUiText(evidence, LOCALE_EN_US))
        assertEquals("谏言", localizeUiText("谏言", LOCALE_ZH_CN))
    }
}
