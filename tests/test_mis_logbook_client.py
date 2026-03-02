import unittest

from mis_logbook_client import LogbookEntry, build_logbook_payload, parse_cas_login_form, parse_logbook_form


CAS_HTML = """
<html>
  <body>
    <form id="fm1" action="/cas/login?service=https://online.mis.pens.ac.id/index.php?Login=1" method="post">
      <input type="text" name="username" value="" />
      <input type="password" name="password" value="" />
      <input type="hidden" name="lt" value="LT-123" />
      <input type="hidden" name="_eventId" value="submit" />
      <input type="submit" name="submit" value="LOGIN" />
    </form>
  </body>
</html>
"""


LOGBOOK_HTML = """
<html>
  <body>
    <form action="submit_logbook.php" method="post">
      <table>
        <tr>
          <td>Tanggal</td>
          <td><input type="text" name="tanggal" value="02-03-2026" /></td>
        </tr>
        <tr>
          <td>Jam Mulai</td>
          <td><input type="text" name="jam_mulai" value="" /></td>
        </tr>
        <tr>
          <td>Jam Selesai</td>
          <td><input type="text" name="jam_selesai" value="" /></td>
        </tr>
        <tr>
          <td>Kegiatan/Materi</td>
          <td><textarea name="kegiatan_materi"></textarea></td>
        </tr>
        <tr>
          <td>Apakah sesuai mata kuliah?</td>
          <td>
            <input type="radio" name="sesuai_matkul" value="1" /> Ya
            <input type="radio" name="sesuai_matkul" value="0" /> Tidak
          </td>
        </tr>
        <tr>
          <td>Jika Ya, Pilih Matakuliah</td>
          <td>
            <select name="matkul">
              <option value="">Pilih Matakuliah</option>
              <option value="RI041105">RI041105 - Workshop Teknologi Web dan Aplikasi</option>
              <option value="RI042106">RI042106 - Praktikum Dasar Pemrograman</option>
            </select>
          </td>
        </tr>
        <tr>
          <td colspan="2">
            <input type="checkbox" name="pernyataan_benar" value="1" />
            Saya menyatakan bahwa data ini benar adanya.
          </td>
        </tr>
      </table>
      <input type="hidden" name="token" value="abc123" />
      <input type="submit" name="submitbtn" value="Simpan" />
    </form>
  </body>
</html>
"""


class MisLogbookClientParserTests(unittest.TestCase):
    def test_parse_cas_login_form_extracts_hidden_fields(self) -> None:
        action_url, payload, error = parse_cas_login_form(
            CAS_HTML,
            "https://login.pens.ac.id/cas/login?service=x",
        )
        self.assertIsNone(error)
        self.assertIsNotNone(action_url)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.get("lt"), "LT-123")
        self.assertEqual(payload.get("_eventId"), "submit")
        self.assertIn("username", payload)
        self.assertIn("password", payload)

    def test_build_logbook_payload_maps_fields_correctly(self) -> None:
        parsed, error = parse_logbook_form(
            LOGBOOK_HTML,
            "https://online.mis.pens.ac.id/mEntry_Logbook_KP1.php",
        )
        self.assertIsNone(error)
        self.assertIsNotNone(parsed)
        assert parsed is not None

        payload, payload_error = build_logbook_payload(
            parsed,
            LogbookEntry(
                date="03-03-2026",
                start_time="08:00",
                end_time="17:00",
                activity="Mengerjakan API backend dan testing endpoint.",
                related=True,
                course_keyword="RI042106",
                agree=True,
            ),
        )
        self.assertIsNone(payload_error)
        self.assertIsNotNone(payload)
        assert payload is not None

        self.assertEqual(payload.get("tanggal"), "03-03-2026")
        self.assertEqual(payload.get("jam_mulai"), "08:00")
        self.assertEqual(payload.get("jam_selesai"), "17:00")
        self.assertIn("API backend", payload.get("kegiatan_materi", ""))
        self.assertEqual(payload.get("sesuai_matkul"), "1")
        self.assertEqual(payload.get("matkul"), "RI042106")
        self.assertEqual(payload.get("pernyataan_benar"), "1")

    def test_build_logbook_payload_fails_when_course_not_found(self) -> None:
        parsed, error = parse_logbook_form(
            LOGBOOK_HTML,
            "https://online.mis.pens.ac.id/mEntry_Logbook_KP1.php",
        )
        self.assertIsNone(error)
        self.assertIsNotNone(parsed)
        assert parsed is not None

        payload, payload_error = build_logbook_payload(
            parsed,
            LogbookEntry(
                date="03-03-2026",
                start_time="08:00",
                end_time="17:00",
                activity="Refactor service layer.",
                related=True,
                course_keyword="RI999999",
                agree=True,
            ),
        )
        self.assertIsNone(payload)
        self.assertIsNotNone(payload_error)
        assert payload_error is not None
        self.assertIn("tidak ditemukan", payload_error.lower())


if __name__ == "__main__":
    unittest.main()
