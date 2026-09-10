package com.mouchen.app.intake

import java.io.ByteArrayInputStream
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertSame
import org.junit.Test

class BoundedInputReaderTest {
    @Test
    fun exactLimitIsAccepted() {
        val source = byteArrayOf(1, 2, 3, 4)
        val result = readBoundedBytes(ByteArrayInputStream(source), 4)

        assertArrayEquals(source, (result as BoundedByteRead.Success).bytes)
    }

    @Test
    fun oneByteOverLimitIsRejected() {
        val result = readBoundedBytes(ByteArrayInputStream(ByteArray(5)), 4)

        assertSame(BoundedByteRead.TooLarge, result)
    }
}
